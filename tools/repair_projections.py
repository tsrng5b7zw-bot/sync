#!/usr/bin/env python3
"""Steady the published projections against last week's, and list the minutes risks the models misread.

Changes the numbers in proj_open.txt and proj_hgb.txt in place (the files as published are kept as
proj_*.raw.local.txt):
  * JUMPS. When a model's forecast for a regular starter's next gameweek is more than 10% above the forecast
    the same model made for that gameweek a week earlier, 70% of the rise is taken back (35% when the forecast
    rose by more than 60%, which is usually a change of status and not of form): in every week for the open
    model, from the second week on for the gradient-boosting model. Backtest GW3-11 of 2026/27: after such a
    rise players scored 0.4 to 1.0 points a week less than the new forecast; on held-out gameweeks the
    pull-back removed that bias, and the best replacement for a regular starter outscored him by 9.1 points
    over five gameweeks against 7.8 without it (better or level in all five windows).
  * --restore 'Name (TEAM);...' sets a player back to last week's forecast in all three models: for a starter
    the models marked down after a short outing that the news explains, such as being taken off after a
    team-mate's red card. Never automatic. Over four seasons (2022/23 to 2025/26) a regular starter who came
    off before the hour started the next match 47% of the time, 62% when a team-mate had been sent off and
    90% after a full game (61% for one who came on as a substitute), and the data cannot tell a tactical change
    from an injury.
Reports without changing anything:
  * SHORT OUTINGS: regular starters who played 1 to 59 minutes in the last gameweek.
  * FLAG CUTS: flagged players whose forecast the open model cut by over 30% in every week. This season such
    cuts were about right for the coming gameweek and a quarter to a half too deep after it, on 30 to 60
    cases: too few to change the numbers, enough to read the news before selling on them.
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backtest as bt
from fplcommon import Data

JUMP, BIG_JUMP, PULL, MIN_PTS = 1.10, 1.60, 0.70, 2.5
FIRST_WEEK = {"open": 1.0, "hgb": 0.0}          # share of PULL applied to the coming gameweek
FILES = {"open": "proj_open.txt", "hgb": "proj_hgb.txt", "house": "proj_house.txt"}
STATE = Path("repair.local.json")


def read(path):
    with open(path, encoding="utf-8") as f:
        head = f.readline().rstrip("\n").split("|")
        return head, [dict(zip(head, line.rstrip("\n").split("|"))) for line in f if line.strip()]


def write(path, head, rows):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("|".join(head) + "\n")
        for r in rows:
            f.write("|".join(r.get(h, "") for h in head) + "\n")
    tmp.replace(path)


def weeks_of(head):
    return sorted(int(h[2:]) for h in head if re.fullmatch(r"gw\d+", h))


def sha(path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def raw_copy(f):
    return Path(f.replace(".txt", ".raw.local.txt"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull")
    ap.add_argument("--squad", help="squad file: its players are listed first and marked *")
    ap.add_argument("--restore", default="", help="'Name (TEAM);...' players to set back to last week's forecast in all three models")
    ap.add_argument("--report-only", action="store_true", help="print what would change and change nothing")
    a = ap.parse_args()

    d = Data(a.data)
    key = {(p["web_name"], p["team"]): el for el, p in d.players.items()}
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for f in FILES.values():                     # a second run starts again from the files as published
        if Path(f).exists() and raw_copy(f).exists() and state.get(f) == sha(f):
            Path(f).write_bytes(raw_copy(f).read_bytes())
    cur = {src: read(f) for src, f in FILES.items() if Path(f).exists()}
    if not cur:
        sys.exit("no projection files here")
    nxt = min(weeks_of(h)[0] for h, _ in cur.values())
    prev = nxt - 1
    boot = json.loads((d.D / "bootstrap.json").read_text(encoding="utf-8"))
    deadlines = {e["id"]: dt.datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00")) for e in boot["events"]}
    restore = [x.strip() for x in a.restore.split(";") if x.strip()]
    squad = set()
    if a.squad and Path(a.squad).exists():
        for line in Path(a.squad).read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(.+?) \((\w+)\)", line)
            if m:
                squad.add(f"{m.group(1)} ({m.group(2)})")

    S = bt.Store()
    if prev >= 1:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                od = bt.clone(bt.OPEN_REPO, ["out/projections_gw*.csv"], tmp, "open")
                bt.load_open(od / "out", deadlines, [prev], S)
            except Exception as ex:
                print(f"! last week's open-model file not read ({type(ex).__name__})")
            try:
                hd = bt.clone(bt.HGB_REPO, ["predictions/archive/*/GW*.csv"], tmp, "hgb")
                bt.load_hgb(sorted(hd.glob("predictions/archive/*/GW*.csv")), deadlines, [prev], S)
            except Exception as ex:
                print(f"! last week's gradient-boosting file not read ({type(ex).__name__})")
        if restore:
            try:
                for g, dd in bt.house_asof(a.data, prev, key).items():
                    S.put("house", prev, g, dd)
            except Exception as ex:
                print(f"! last week's house forecast not rebuilt ({type(ex).__name__})")

    def last_week(src, el, g):
        return S.proj[src].get((prev, g), {}).get(el)

    def steady(src, row, weeks, force):
        el = key.get((row.get("name"), row.get("team")))
        post = [num(row.get(f"gw{g}")) for g in weeks]
        if el is None or post[0] is None:
            return None
        pre0 = last_week(src, el, weeks[0])
        if pre0 is None:
            return None
        q = post[0] / max(pre0, 0.1)
        if not force and not ((pre0 + post[0]) / 2 >= MIN_PTS and q > JUMP):
            return None
        pull = 1.0 if force else (PULL / 2 if q > BIG_JUMP else PULL)
        out, ratio = [], 1.0
        for k, g in enumerate(weeks):
            if post[k] is None:
                out.append(None)
                continue
            pre = last_week(src, el, g)
            if pre is None:
                out.append(post[k] * ratio)       # a week last week's file did not reach: same proportion
                continue
            w = pull * (FIRST_WEEK[src] if k == 0 and not force else 1.0)
            new = post[k] - w * (post[k] - pre)
            if post[k] > 0:
                ratio = new / post[k]
            out.append(new)
        return out

    five = lambda v: sum(x for x in v[:5] if x is not None)
    stats, done = [], set()
    for src in ("open", "hgb", "house"):
        if src not in cur:
            continue
        if not S.has(src, prev):
            if src != "house":
                print(f"! no {src} file from last week: left as published")
            continue
        head, rows = cur[src]
        weeks, touched = weeks_of(head), 0
        for r in rows:
            who = f"{r.get('name')} ({r.get('team')})"
            force = who in restore
            if src == "house" and not force:
                continue
            new = steady(src, r, weeks, force)
            if new is None:
                continue
            before = [num(r.get(f"gw{g}")) for g in weeks]
            stats.append((five(new) - five(before), who, src, five(before), five(new), force))
            if force:
                done.add(who)
            for g, x in zip(weeks, new):
                if x is not None:
                    r[f"gw{g}"] = f"{x:.2f}"
            if "nextn" in r:
                r["nextn"] = f"{sum(x for x in new if x is not None):.1f}"
            touched += 1
        if touched and not a.report_only:
            raw_copy(FILES[src]).write_bytes(Path(FILES[src]).read_bytes())
            write(FILES[src], head, rows)
            state[FILES[src]] = sha(FILES[src])
    if not a.report_only:
        STATE.write_text(json.dumps(state))

    jumps = [s for s in stats if not s[5]]
    print(f"JUMPS steadied: {sum(1 for s in jumps if s[2] == 'open')} in the open model, {sum(1 for s in jumps if s[2] == 'hgb')} in the "
          f"gradient-boosting model (forecast up over 10% in a week; 70% of the rise taken back, 35% above a 60% rise)")
    for s in sorted(jumps, key=lambda s: (s[1] not in squad, s[0]))[:12]:
        print(f"  {'*' if s[1] in squad else ' '} {s[1]:24s} {s[2]:5s} {s[3]:5.1f} -> {s[4]:5.1f} over five gameweeks")
    for s in stats:
        if s[5]:
            print(f"RESTORED {s[1]} in the {s[2]} model: {s[3]:.1f} -> {s[4]:.1f} over five gameweeks (last week's forecast)")
    for who in restore:
        if who not in done:
            print(f"! --restore {who}: not found in the files or in last week's forecasts")

    lastg = d.last_finished_gw()
    if lastg != prev:
        print(f"(short outings and flag cuts skipped: last finished gameweek is {lastg}, the files start at GW{nxt})")
        return
    pl = {int(r["id"]): r for r in csv.DictReader(open(d.D / "players.csv", encoding="utf-8"))}
    mins = defaultdict(int)
    for r in csv.DictReader(open(d.D / "player_gw_history.csv", encoding="utf-8")):
        mins[(int(r["id"]), int(r["round"]))] += int(float(r["minutes"] or 0))
    reds = defaultdict(list)
    live = d.D / f"live_gw{lastg}.csv"
    if live.exists():
        for r in csv.DictReader(open(live, encoding="utf-8")):
            if int(float(r.get("red_cards") or 0)) > 0:
                reds[r["team"]].append(r["web_name"])
    flag = lambda p: "no flag" if p["status"] == "a" and p["chance_of_playing_next_round"] in ("", "100") else \
        f"flagged {p['chance_of_playing_next_round'] or p['status']}{'%' if p['chance_of_playing_next_round'] else ''}"
    outs = []
    for el, p in pl.items():
        m = mins.get((el, lastg), 0)
        if 0 < m < 60 and lastg >= 4 and all(mins.get((el, g), 0) >= 60 for g in range(lastg - 3, lastg)):
            who = f"{p['web_name']} ({p['team']})"
            mates = [x for x in reds.get(p["team"], []) if x != p["web_name"]]
            note = "sent off" if p["web_name"] in reds.get(p["team"], []) else (f"team-mate sent off ({', '.join(mates)})" if mates else "no red card")
            outs.append((who not in squad, -(last_week("open", el, nxt) or 0), f"  {'*' if who in squad else ' '} {who:24s} {m:2d} min; {note}; {flag(p)}; "
                         f"forecast for GW{nxt} a week ago {last_week('open', el, nxt) or 0:.1f}, now {num(next((r.get(f'gw{nxt}') for r in cur.get('open', ([], []))[1] if r.get('name') == p['web_name'] and r.get('team') == p['team']), None)) or 0:.1f} (open model)"))
    print(f"SHORT OUTINGS in GW{lastg} by regular starters ({len(outs)}): over four seasons 55% of these started the next match (48% of those taken off "
          f"before the hour, 61% of those who came on as a substitute, 62% when a team-mate was sent off) against 90% after a full game. "
          f"Read the news before buying, selling or --restore.")
    for o in sorted(outs)[:14]:
        print(o[2])
    cuts = []
    if "open" in cur and S.has("open", prev):
        head, rows = cur["open"]
        weeks = weeks_of(head)[:5]
        for r in rows:
            el = key.get((r.get("name"), r.get("team")))
            p = pl.get(el)
            if p is None or flag(p) == "no flag":
                continue
            pre = [last_week("open", el, g) for g in weeks]
            now = [num(r.get(f"gw{g}")) for g in weeks]
            if pre[0] is None or pre[0] < MIN_PTS or None in pre or None in now or sum(now) >= 0.7 * sum(pre):
                continue
            who = f"{r['name']} ({r['team']})"
            cuts.append((who not in squad, -sum(pre), f"  {'*' if who in squad else ' '} {who:24s} {flag(p)}: {sum(pre):.1f} -> {sum(now):.1f} over five gameweeks; {p['news'][:70]}"))
    print(f"FLAG CUTS ({len(cuts)}): flagged regulars the open model has cut by over 30% in every week; later weeks may be too low.")
    for c in sorted(cuts)[:12]:
        print(c[2])


if __name__ == "__main__":
    main()
