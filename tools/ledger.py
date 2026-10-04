#!/usr/bin/env python3
"""
Keep score of the advice. Every pre-deadline call is written down before the matches — the team
recommended, the alternative it beat, and what each source projected — and scored once the
gameweek is over, on the points those exact lineups actually produced. Over a season this is the
only honest answer to "is the process adding points?".

  python tools/ledger.py add --gw 6 --pick saved.local.txt --alt package.local.txt --note "hold vs Mbeumo package" \\
         --proj-pick "fplform 287.2, blend 277.4" --proj-alt "fplform 284.8, blend 281.7"
  python tools/ledger.py score            # fills in actual points for every finished gameweek
  python tools/ledger.py show             # the table
  python tools/ledger.py benchmark --me <alias> --frozen frozen.local.txt --from 6
        # your real score each week against the same 15 left untouched since GW<from>: the saved XI,
        # captain and bench order, auto-subs only, no transfers, no chips. The difference is what
        # the management (transfers, lineups, chips) has been worth.

A pick or alt file is a lineup: 11 starters as 'web_name (TEAM)', one per line, the captain's line
ending in ' (C)'; a bench is optional and ignored (auto-subs are not replayed, so a lineup with a
non-starter is scored with the zero he produced — the mistake the ledger is meant to catch).
A frozen file is the same with the bench included: 11 starters, then the goalkeeper substitute,
then the three outfield substitutes in bench order (15 lines); the vice-captain's line may end
in ' (V)'. The ledger lives in ledger.local.json, which the repo ignores.
"""
import argparse, datetime as dt, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, read_csv

LEDGER = "ledger.local.json"


def read_lineup(path, key):
    xi, cap = [], None
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        s = ln.split("#", 1)[0].strip()
        if not s:
            continue
        is_cap = s.endswith("(C)")
        s = s[:-3].strip() if is_cap else s
        el = key.get(s)
        if el is None:
            raise SystemExit(f"{path}: cannot resolve {s!r}; write 'web_name (TEAM)' exactly as players.csv has it")
        xi.append(el)
        if is_cap:
            cap = el
    if len(xi) < 11:
        raise SystemExit(f"{path}: a lineup needs 11 starters, got {len(xi)}")
    return {"xi": xi[:11], "captain": cap or xi[0], "labels": [s for s in
            (ln.split("#", 1)[0].strip().replace("(C)", "").strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()) if s][:11]}


def load(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save(path, rows):
    Path(path).write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def points(lineup, pts):
    tot = 0.0
    for el in lineup["xi"]:
        p = pts.get(el, 0.0)
        tot += p * (2 if el == lineup["captain"] else 1)
    return tot


def read_frozen(path, key, d):
    """15 lines: XI (captain ' (C)', optional vice ' (V)'), then GK sub, then three outfield subs."""
    xi, bench, cap, vice = [], [], None, None
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        s = ln.split("#", 1)[0].split("@", 1)[0].strip()
        if not s:
            continue
        tag = None
        for t in ("(C)", "(V)"):
            if s.endswith(t):
                tag, s = t, s[:-3].strip()
        el = key.get(s)
        if el is None:
            raise SystemExit(f"{path}: cannot resolve {s!r}; write 'web_name (TEAM)' exactly as players.csv has it")
        (xi if len(xi) < 11 else bench).append(el)
        if tag == "(C)":
            cap = el
        elif tag == "(V)":
            vice = el
    if len(xi) != 11 or len(bench) != 4:
        raise SystemExit(f"{path}: a frozen team needs 11 starters then 4 substitutes, got {len(xi)} + {len(bench)}")
    pos = {el: d.players[el]["pos"] for el in xi + bench}
    if pos[bench[0]] != "GK":
        raise SystemExit(f"{path}: the first substitute must be the goalkeeper")
    return {"xi": xi, "bench": bench, "captain": cap or xi[0], "vice": vice, "pos": pos}


def frozen_week(team, gw, pts, mins):
    """FPL's auto-subs for one gameweek: a starter with no minutes is replaced by the first
    substitute, in bench order, who played and keeps the formation legal (1 GK, 3+ DEF, 2+ MID,
    1+ FWD). The captain's armband passes to the vice if the captain did not play."""
    played = lambda el: mins.get(el, 0) > 0
    pos = team["pos"]
    lineup = list(team["xi"])
    used = set()
    if not played(lineup[0]) and played(team["bench"][0]):
        lineup[0] = team["bench"][0]; used.add(team["bench"][0])
    def legal(l):
        c = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
        for el in l:
            c[pos[el]] += 1
        return c["GK"] == 1 and c["DEF"] >= 3 and c["MID"] >= 2 and c["FWD"] >= 1
    for el in list(lineup):
        if pos[el] == "GK" or played(el):
            continue
        for b in team["bench"][1:]:
            if b in used or not played(b):
                continue
            trial = [b if x == el else x for x in lineup]
            if legal(trial):
                lineup = trial; used.add(b); break
    cap = team["captain"] if played(team["captain"]) else (team["vice"] if team["vice"] and played(team["vice"]) else None)
    return sum(pts.get(el, 0) * (2 if el == cap else 1) for el in lineup)


def benchmark(a, d, key):
    if not (a.me and a.frozen):
        sys.exit("benchmark needs --me and --frozen")
    team = read_frozen(a.frozen, key, d)
    pts, mins = {}, {}
    for r in read_csv(d.D / "player_gw_history.csv"):
        g, el = int(r["round"]), int(r["id"])
        pts.setdefault(g, {})[el] = pts.get(g, {}).get(el, 0) + int(float(r["total_points"] or 0))
        mins.setdefault(g, {})[el] = mins.get(g, {}).get(el, 0) + int(float(r["minutes"] or 0))
    cur = d.history(d.resolve_entry(a.me)).get("current", [])
    actual = {int(c["event"]): int(c["points"]) for c in cur}
    last = d.last_finished_gw() or 0
    print(f"{'GW':4}{'you':>6}{'frozen':>8}{'diff':>6}   (you = real score, chips and hits included; frozen = the GW{a.start} team untouched)")
    tot_you = tot_fr = 0
    for g in range(a.start, last + 1):
        if g not in pts or g not in actual:
            continue
        fr = frozen_week(team, g, pts[g], mins[g]); you = actual[g]
        tot_you += you; tot_fr += fr
        print(f"{g:<4}{you:6}{fr:8}{you - fr:+6}")
    n = len([g for g in range(a.start, last + 1) if g in pts and g in actual])
    if not n:
        print(f"no gameweek finished since GW{a.start} yet")
        return
    print(f"\n{'all':4}{tot_you:6}{tot_fr:8}{tot_you - tot_fr:+6}   {(tot_you - tot_fr) / n:+.1f} a week over {n} gameweek(s): the value of every decision since GW{a.start}. Read it after ten.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["add", "score", "show", "benchmark"])
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--gw", type=int); ap.add_argument("--pick"); ap.add_argument("--alt")
    ap.add_argument("--note", default=""); ap.add_argument("--proj-pick", default=""); ap.add_argument("--proj-alt", default="")
    ap.add_argument("--me", help="your alias (benchmark)"); ap.add_argument("--frozen", help="the untouched team, 15 lines (benchmark)")
    ap.add_argument("--from", dest="start", type=int, default=1, help="first gameweek the frozen team stands for (benchmark)")
    a = ap.parse_args()
    d = Data(a.data)
    key = {f"{p['web_name']} ({p['team']})": el for el, p in d.players.items()}
    if a.cmd == "benchmark":
        benchmark(a, d, key)
        return
    rows = load(a.ledger)
    if a.cmd == "add":
        if not (a.gw and a.pick and a.alt):
            sys.exit("add needs --gw, --pick and --alt")
        rows.append({"gw": a.gw, "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"), "note": a.note,
                     "pick": read_lineup(a.pick, key), "alt": read_lineup(a.alt, key),
                     "proj_pick": a.proj_pick, "proj_alt": a.proj_alt, "actual_pick": None, "actual_alt": None})
        save(a.ledger, rows)
        print(f"GW{a.gw} recorded: {a.note or 'pick vs alt'}")
        return
    if a.cmd == "score":
        last = d.last_finished_gw() or 0
        pts = {}
        for r in read_csv(d.D / "player_gw_history.csv"):
            pts.setdefault(int(r["round"]), {}).setdefault(int(r["id"]), 0.0)
            pts[int(r["round"])][int(r["id"])] += float(r["total_points"] or 0)
        n = 0
        for row in rows:
            if row["gw"] <= last and row["gw"] in pts and row["actual_pick"] is None:
                row["actual_pick"] = points(row["pick"], pts[row["gw"]])
                row["actual_alt"] = points(row["alt"], pts[row["gw"]])
                n += 1
        save(a.ledger, rows)
        print(f"scored {n} gameweek(s)")
    scored = [r for r in rows if r["actual_pick"] is not None]
    print(f"{'GW':4}{'date':11}{'pick':>7}{'alt':>7}{'edge':>7}  note / projections")
    for r in rows:
        if r["actual_pick"] is None:
            print(f"{r['gw']:<4}{r['date']:11}{'-':>7}{'-':>7}{'-':>7}  {r['note']}  [{r['proj_pick']} | {r['proj_alt']}]")
        else:
            print(f"{r['gw']:<4}{r['date']:11}{r['actual_pick']:7.0f}{r['actual_alt']:7.0f}{r['actual_pick'] - r['actual_alt']:+7.0f}  {r['note']}  [{r['proj_pick']} | {r['proj_alt']}]")
    if scored:
        edge = sum(r["actual_pick"] - r["actual_alt"] for r in scored)
        wins = sum(1 for r in scored if r["actual_pick"] > r["actual_alt"])
        print(f"\n{len(scored)} scored: the recommended lineup beat the alternative {wins} times, net {edge:+.0f} points"
              f" ({edge / len(scored):+.1f} a week). One week is noise; read this after ten.")


if __name__ == "__main__":
    main()
