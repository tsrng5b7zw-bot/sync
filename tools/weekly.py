#!/usr/bin/env python3
"""
The whole weekly run in one command, so an unattended check-in cannot skip a step.

  python tools/weekly.py --me <alias> --squad my_squad.local.txt --bank 0.3 --names names.local.json
         [--fplform proj_raw.txt] [--committed WC] [--transfers 1] [--candidate alt.local.txt ...]
         [--ledger ledger.local.json --frozen frozen.local.txt --from 6] [--watch "Name (TEAM);..."]
         [--archive /path/that/survives/recloning] [--no-fetch] [--no-sim] [--out weekly.local.txt]

What it does, in order, printing each step's result and a SUMMARY at the end:
  1. fetch the collector's latest data (tools/fetch_latest.py) and say how old it is;
  2. build the projections: fplform from --fplform when that file exists (read in the browser
     with tools/fplform_snippets.md), always the no-browser blend (open model, house model,
     gradient-boosting model, whichever answer), and the consensus of the two when both exist;
     each source is dated, and the archive copy is saved for tools/backtest.py;
  3. on EVERY source: holding the squad, the best 1, 2 and 3 changes, the best possible squad
     (the ceiling), and the next gameweek's XI and captain;
  4. every move any source proposed, plus every --candidate, scored on every source with the same
     test (the rule: with fplform in hand a move must not lose on either source and must clear
     2 points on the consensus; without it, 3 points on the blend with every model agreeing);
  5. the league simulation (tools/run_simulation.py, skill tiered) for the squad and the candidates;
  6. the checks: price moves in view, blanks and doubles, who might not play;
  7. the ledger score and the frozen-team benchmark when those files are given.

The report is only numbers and verdicts; the message is still written from it by hand, following
the rules in the skill. Each step is one of the existing tools run unchanged, so anything here can
be re-run on its own. A failed step is reported and the run continues with what remains.
"""
import argparse, datetime as dt, json, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fplcommon import Data, Projections, read_squad_file, read_purchases  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None

NOISE_WITH_FPLFORM = 2.0      # consensus points a move must clear when fplform is in hand
NOISE_BLEND_ONLY = 3.0        # blend points a move must clear with no browser


def run(args, timeout=1800):
    """Run a sibling tool; return (returncode, combined output)."""
    cmd = [sys.executable, str(HERE / args[0])] + [str(x) for x in args[1:]]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr.strip() else "")
        return p.returncode, out.strip(), time.time() - t0
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s", time.time() - t0


def move_label(out, inn):
    """'Van Hecke (TOT) → Botman (NEW)' for one change; 'OUT a, b → IN x, y' for a package."""
    if len(out) == 1 and len(inn) == 1:
        return f"{out[0]} → {inn[0]}"
    return f"OUT {', '.join(out)} → IN {', '.join(inn)}"


def section(title):
    print("\n" + "=" * 100 + f"\n{title}\n" + "=" * 100)


def tail(text, n=12):
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def fmt_local(utc_iso):
    """'Sat 10 Oct 10:00 UTC (05:00 Chicago)' from an ISO timestamp."""
    try:
        t = dt.datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    except Exception:
        return utc_iso
    s = t.strftime("%a %d %b %H:%M UTC")
    if ZoneInfo:
        s += " (" + t.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M %Z Chicago") + ")"
    return s


def age(utc_iso):
    try:
        t = dt.datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        h = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600
        return f"{h:.1f} h old"
    except Exception:
        return "age unknown"


# ---- parsers for the tools' output ---------------------------------------------------------

RE_TOTAL = re.compile(r"^(Hold \(no transfers\)|Best with up to (\d+) transfer\(s\)|Best squad.*?) \| projected ([\d.]+) over (\d+) GW \((gw\d+)-(gw\d+)\)")
RE_GAIN = re.compile(r"^gain over holding: ([+-][\d.]+) projected points")
RE_CAPS = re.compile(r"^captains: (.*)$")


def parse_optimize(out):
    """{'hold': total, 'best': total, 'gain': x, 'out': [...], 'in': [...], 'window': 'gw6-gw10', 'n_gw': 5,
        'rows': [...], 'captains': {...}, 'error': str|None}"""
    r = {"hold": None, "best": None, "gain": None, "out": [], "in": [], "window": None, "n_gw": None,
         "rows": [], "captains": {}, "error": None, "notes": []}
    block = None
    for ln in out.splitlines():
        m = RE_TOTAL.match(ln)
        if m:
            block = "hold" if m.group(1).startswith("Hold") else "best"
            r[block] = float(m.group(3))
            r["n_gw"] = int(m.group(4)); r["window"] = f"{m.group(5)}-{m.group(6)}"
            if block == "best":
                r["rows"] = []
            continue
        if ln.startswith("OUT:"):
            r["out"] = [s.strip() for s in ln[4:].split(",") if s.strip()]
        elif ln.startswith("IN :"):
            r["in"] = [s.strip() for s in ln[4:].split(",") if s.strip()]
        elif RE_GAIN.match(ln):
            r["gain"] = float(RE_GAIN.match(ln).group(1))
        elif RE_CAPS.match(ln) and block in ("best", "hold"):
            caps = {}
            for part in RE_CAPS.match(ln).group(1).split(","):
                if ":" in part:
                    g, n = part.strip().split(":", 1); caps[g] = n
            r["captains"] = caps
        elif ln[:4].strip() in ("GK", "DEF", "MID", "FWD") and len(ln) > 46 and block:
            try:
                row = {"pos": ln[0:4].strip(), "label": ln[4:30].strip(), "cost": float(ln[30:35]),
                       "proj": float(ln[35:41]), "st": int(ln[41:44]), "cap": int(ln[44:46]),
                       "in": ln.rstrip().endswith(" IN")}
                gws = ln[46:].replace(" IN", "").split()
                row["gw"] = [float(x) for x in gws if re.match(r"^-?\d+(\.\d+)?$", x)]
                r["rows"].append(row)
            except ValueError:
                pass
        elif ln.startswith("!") or ln.startswith("no projection") or "unmatched" in ln.lower():
            r["notes"].append(ln.strip())
    if r["hold"] is None and r["best"] is None:
        r["error"] = tail(out, 6)
    if r["best"] is None and r["hold"] is not None:      # --transfers 0: the hold is the answer
        r["best"] = r["hold"]; r["gain"] = 0.0
    return r


RE_FORECAST = re.compile(r"^skill (ignored|tiered)\s+([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)")


def parse_simulation(out):
    r = {"forecast": {}, "candidates": [], "error": None}
    in_cand = False
    for ln in out.splitlines():
        m = RE_FORECAST.match(ln)
        if m:
            r["forecast"][m.group(1)] = {"window": float(m.group(2)), "p1": float(m.group(3)), "p2": float(m.group(4)),
                                         "p3": float(m.group(5)), "p4": float(m.group(6)), "top3": float(m.group(7)),
                                         "money": float(m.group(8)), "ev": float(m.group(9))}
            continue
        if ln.startswith("== CANDIDATES"):
            in_cand = True; continue
        if in_cand:
            m2 = re.match(r"^(.+?)\s+([\d.]+)\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)%\s+([\d.]+)\s+([+-][\d.]+)$", ln.strip())
            if m2:
                r["candidates"].append({"name": m2.group(1).strip(), "window": float(m2.group(2)), "p1": float(m2.group(3)),
                                        "top3": float(m2.group(7)), "money": float(m2.group(8)), "ev": float(m2.group(9)),
                                        "vs": float(m2.group(10))})
            elif ln.startswith("=="):
                in_cand = False
    if not r["forecast"]:
        r["error"] = tail(out, 8)
    return r


# ---- the run -------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--me", required=True, help="your alias in the data")
    ap.add_argument("--squad", required=True, help="your 15, 'web_name (TEAM) [@price paid]' per line")
    ap.add_argument("--bank", type=float, required=True, help="Money Remaining in the app")
    ap.add_argument("--names", help="names.local.json for the simulator's table")
    ap.add_argument("--fplform", default="proj_raw.txt", help="fplform table saved from the browser (skipped when missing)")
    ap.add_argument("--committed", help="chip already played for the next deadline: WC, FH, BB or TC")
    ap.add_argument("--transfers", default="1", help="free transfers in hand, or WC for unlimited (default 1)")
    ap.add_argument("--candidate", action="append", default=[], help="alternative squad file; repeatable")
    ap.add_argument("--ledger", help="ledger.local.json")
    ap.add_argument("--frozen", help="frozen team file for ledger.py benchmark")
    ap.add_argument("--from", dest="start", type=int, default=6, help="first gameweek of the benchmark")
    ap.add_argument("--watch", default="", help="'Name (TEAM);...' extra players for the price and starter checks")
    ap.add_argument("--data", default="data_pull")
    ap.add_argument("--archive", default="archive", help="where archive_projections.py keeps the dated copies (a path outside the clone survives re-cloning)")
    ap.add_argument("--repo", default="tsrng5b7zw-bot/sync")
    ap.add_argument("--no-fetch", action="store_true", help="use the data already in --data")
    ap.add_argument("--no-sim", action="store_true", help="skip the simulator (slow)")
    ap.add_argument("--max-reserves", type=int, default=1)
    ap.add_argument("--out", help="also write the whole report to this file")
    a = ap.parse_args()

    log = []
    class Tee:
        def __init__(self, stream): self.stream = stream
        def write(self, s): self.stream.write(s); log.append(s)
        def flush(self): self.stream.flush()
    sys.stdout = Tee(sys.stdout)

    started = dt.datetime.now(dt.timezone.utc)
    print(f"weekly run {started.strftime('%Y-%m-%d %H:%M UTC')}")
    failures, summary = [], {}

    # 1. data ------------------------------------------------------------------------------
    section("1. DATA")
    if not a.no_fetch:
        rc, out, secs = run(["fetch_latest.py", a.repo, "--dest", a.data])
        print(tail(out, 3))
        if rc:
            failures.append("fetch_latest"); print("! fetch failed; using the data already on disk")
    data = Data(a.data)
    pulled = data.meta.get("pulled_at_utc", "?")
    deadline = data.meta.get("next_deadline_utc", "?")
    print(f"data pulled {pulled} ({age(pulled)}); GW{data.cur_gw} finished: {data.fixtures_finished()}; next GW{data.nxt_gw}, deadline {fmt_local(deadline)}")
    try:
        hrs = float(age(pulled).split()[0])
        if hrs > 6:
            print(f"! data {hrs:.0f} h old: GitHub skips scheduled runs under load; use it, and lean on an injury-news search")
    except ValueError:
        pass
    summary["data"] = f"pulled {pulled} ({age(pulled)}), deadline {fmt_local(deadline)}"

    squad_ids = read_squad_file(a.squad, data)
    purchases = read_purchases(a.squad, data)
    print(f"squad: {len(squad_ids)} players, purchase notes for {len(purchases)}, bank £{a.bank:.1f}m")
    if len(squad_ids) != 15:
        print(f"! the squad file has {len(squad_ids)} players, not 15")

    # 2. projections -----------------------------------------------------------------------
    section("2. PROJECTIONS")
    sources = {}      # name -> file
    fpl_path = Path(a.fplform)
    use_fplform = fpl_path.exists()
    if use_fplform:
        mtime = dt.datetime.fromtimestamp(fpl_path.stat().st_mtime, dt.timezone.utc)
        cur_dl = data.meta.get("current_deadline_utc")
        try:
            if cur_dl and mtime < dt.datetime.fromisoformat(cur_dl.replace("Z", "+00:00")):
                print(f"! {fpl_path} was saved {mtime.strftime('%d %b %H:%M UTC')}, before the last deadline: a stale read. "
                      f"Ignoring it; re-read fplform in the browser or delete the file")
                use_fplform = False
        except ValueError:
            pass
    if use_fplform:
        rc, out, secs = run(["build_dataset.py", "--data", a.data, "--proj", str(fpl_path), "--me", a.me])
        print(tail(out, 6))
        if rc:
            failures.append("build_dataset(fplform)"); print("! build_dataset failed on the fplform file")
        else:
            sources["fplform"] = "proj.txt"
            print(f"fplform table {fpl_path} saved {mtime.strftime('%Y-%m-%d %H:%M UTC')} ({age(mtime.isoformat())})")
            days = (dt.datetime.now(dt.timezone.utc) - mtime).days
            if days >= 5:
                print(f"! that fplform read is {days} days old: re-read it in the browser (tools/fplform_snippets.md) or delete the file to run blend-only")
    else:
        print(f"no {a.fplform}: no fplform this run (browser read not done); the blend decides")
    blend_inputs = []
    for tool, f in (("fetch_open_projections.py", "proj_open.txt"), ("house_projections.py", "proj_house.txt"),
                    ("fetch_hgb_projections.py", "proj_hgb.txt")):
        rc, out, secs = run([tool, "--data", a.data])
        line = tail(out, 1)
        print(("ok  " if rc == 0 else "FAIL") + f" {tool}: {line}")
        if rc == 0 and Path(f).exists():
            blend_inputs.append(f)
        else:
            failures.append(tool)
    if blend_inputs:
        weights = {"proj_open.txt": 2, "proj_house.txt": 1, "proj_hgb.txt": 1}
        w = ",".join(str(weights[f]) for f in blend_inputs) if len(blend_inputs) == 3 else ",".join("1" for _ in blend_inputs)
        rc, out, secs = run(["combine_projections.py", *blend_inputs, "--weights", w, "--out", "proj_blend.txt"])
        print(tail(out, 1))
        if rc == 0:
            sources["blend"] = "proj_blend.txt"
            if len(blend_inputs) < 3:
                print(f"! blend from {len(blend_inputs)} of 3 models, equal weights: {', '.join(blend_inputs)}")
        else:
            failures.append("combine(blend)")
    if "fplform" in sources and "blend" in sources:
        rc, out, secs = run(["combine_projections.py", "proj.txt", "proj_blend.txt", "--common", "--out", "proj_cons.txt"])
        print(tail(out, 1))
        if rc == 0:
            sources["consensus"] = "proj_cons.txt"
    if not sources:
        print("! no projections at all; stopping"); sys.exit(2)
    if "fplform" not in sources:
        # actuals.txt and league_summary.txt without writing a proj.txt that the archive would take for fplform
        stale = Path("proj.txt")
        if stale.exists():
            stale.rename("proj.stale.local.txt")
            print("renamed the old proj.txt to proj.stale.local.txt so it is not archived as this week's fplform")
        rc, out, secs = run(["build_dataset.py", "--data", a.data, "--proj", "no-fplform-this-run", "--me", a.me])
        print(tail(out, 2))
    rc, out, secs = run(["archive_projections.py", "--data", a.data, "--archive", a.archive])
    print(tail(out, 1))
    decider = "consensus" if "consensus" in sources else ("blend" if "blend" in sources else "fplform")
    summary["sources"] = ", ".join(sources); summary["decider"] = decider
    print(f"sources this run: {', '.join(sources)}; decisions on the {decider}")

    # 3. optimiser on every source -----------------------------------------------------------
    section("3. SQUAD, EVERY SOURCE")
    base = ["optimize.py", "--data", a.data, "--squad", a.squad, "--bank", f"{a.bank:.1f}", "--me", a.me,
            "--max-reserves", str(a.max_reserves)]
    free = str(a.transfers).upper() == "WC"
    n_free = 3 if free else max(0, int(a.transfers))
    results = {}
    windows = {}
    for src, f in sources.items():
        results[src] = {}
        for t in [0, 1, 2, 3]:
            rc, out, secs = run(base + ["--proj", f, "--transfers", str(t)])
            r = parse_optimize(out)
            if r["error"]:
                failures.append(f"optimize({src},{t})"); print(f"! {src} transfers={t}: {r['error']}")
            results[src][t] = r
            windows[src] = (r["n_gw"], r["window"])
        rc, out, secs = run(base + ["--proj", f])              # the ceiling: any 15
        results[src]["ceiling"] = parse_optimize(out)
        rc, out, secs = run(base + ["--proj", f, "--transfers", "0", "--horizon", "1"])
        results[src]["xi"] = parse_optimize(out)
        hold = results[src][0]["hold"]
        ceiling = results[src]["ceiling"]["best"]
        n_gw, win = windows[src]
        print(f"\n{src} ({win}, {n_gw} GW): hold {hold:.1f}; ceiling {ceiling:.1f} ({(ceiling or 0) - (hold or 0):+.1f} for a full rebuild)" if hold is not None and ceiling is not None else f"\n{src}: hold/ceiling unavailable")
        for t in (1, 2, 3):
            r = results[src][t]
            if r["gain"] is not None and (r["out"] or r["in"]):
                print(f"  best {t}: {r['gain']:+.1f}  OUT {', '.join(r['out'])}  IN {', '.join(r['in'])}")
            elif r["gain"] is not None:
                print(f"  best {t}: no change beats holding")
        for note in results[src][0]["notes"][:4]:
            print("  ", note)
        xi = results[src]["xi"]
        if xi["rows"]:
            starters = [row for row in xi["rows"] if row["st"] >= 1]
            cap = next((row["label"] for row in xi["rows"] if row["cap"] >= 1), "?")
            print(f"  next GW XI ({len(starters)}): " + ", ".join(row["label"] for row in starters))
            print(f"  captain: {cap}")

    # 4. every proposed move on every source ---------------------------------------------------
    section("4. EVERY OPTION, SAME TEST")
    moves = {}     # key -> {'out': [...], 'in': [...], 'label': str}
    for src in sources:
        for t in (1, 2, 3):
            r = results[src][t]
            if r["out"] and r["in"] and len(r["out"]) <= n_free + (0 if free else 0) + 3:
                key = (tuple(sorted(r["out"])), tuple(sorted(r["in"])))
                moves.setdefault(key, {"out": list(r["out"]), "in": list(r["in"]), "label": move_label(r["out"], r["in"]),
                                       "from": set()})
                moves[key]["from"].add(src)
    cand_files = {}
    for cf in a.candidate:
        try:
            ids = read_squad_file(cf, data)
        except SystemExit as e:
            print(f"! candidate {cf}: {e}"); continue
        out_ids = [i for i in squad_ids if i not in ids]; in_ids = [i for i in ids if i not in squad_ids]
        key = (tuple(sorted(data.label(i) for i in out_ids)), tuple(sorted(data.label(i) for i in in_ids)))
        moves.setdefault(key, {"out": [data.label(i) for i in out_ids], "in": [data.label(i) for i in in_ids],
                               "label": Path(cf).name, "from": set()})
        moves[key]["from"].add("candidate"); cand_files[key] = cf
    common_gw = min(w[0] for w in windows.values() if w[0]) if windows else None
    test_sources = dict(sources)
    if "fplform" not in sources:        # no browser: every model behind the blend must agree too
        for f in blend_inputs:
            test_sources[f.replace("proj_", "").replace(".txt", "")] = f
    table = []
    for key, mv in moves.items():
        row = {"label": mv["label"], "n": len(mv["out"]), "gains": {}, "from": mv["from"]}
        for src, f in test_sources.items():
            cmd = base + ["--proj", f, "--transfers", str(len(mv["out"])), "--must", ";".join(mv["in"]), "--ban", ";".join(mv["out"])]
            if common_gw:
                cmd += ["--horizon", str(common_gw)]
            rc, out, secs = run(cmd)
            r = parse_optimize(out)
            got = set(r["in"]) >= set(mv["in"]) and set(r["out"]) >= set(mv["out"])
            row["gains"][src] = r["gain"] if (r["gain"] is not None and got) else None
            if r["gain"] is not None and not got and not r["error"]:
                row["gains"][src] = None; row.setdefault("notes", []).append(f"{src}: could not be built (money or club limit)")
        table.append(row)
    if table:
        head = f"{'move':72}" + "".join(f"{s:>12}" for s in test_sources) + "   verdict"
        print(head)
        for row in table:
            verdict = []
            g = {s: (v + 0.0 if v is not None else None) for s, v in row["gains"].items()}
            row["gains"] = g
            hits = 0 if free else max(0, row["n"] - n_free)
            cost = 4 * hits
            row["hits"] = hits
            if any(v is None for v in g.values()):
                verdict.append("not buildable on " + ", ".join(s for s, v in g.items() if v is None))
                row["net"] = None
            else:
                dec_src = "consensus" if "consensus" in g else ("blend" if "blend" in g else list(g)[0])
                net = g[dec_src] - cost
                row["net"] = net
                bar = NOISE_WITH_FPLFORM if ("fplform" in g and "blend" in g) else NOISE_BLEND_ONLY
                judged = ("fplform", "blend") if "fplform" in g else tuple(k for k in g if k != "consensus")
                losing = [s for s in judged if s in g and g[s] - cost < 0]
                flat = [s for s in judged if s in g and 0 <= g[s] - cost < 1.0]
                if hits:
                    verdict.append(f"net {net:+.1f} after {hits} hit(s)")
                if losing:
                    verdict.append(("loses on " if "fplform" in g else "a model disagrees: ") + ", ".join(f"{s} {g[s] - cost:+.1f}" for s in losing))
                if net <= bar:
                    verdict.append(f"inside noise (≤{bar:.0f} on {dec_src})")
                if not losing and net > bar:
                    verdict.append("CLEARS THE BAR" + (f", but flat on {' and '.join(flat)}: a marginal pass, decide on the underlying numbers" if flat else ""))
            row["verdict"] = "; ".join(verdict)
            print(f"{row['label'][:72]:72}" + "".join(f"{(('%+.1f' % g[s]) if g[s] is not None else '—'):>12}" for s in test_sources) + "   " + row["verdict"])
            for note in row.get("notes", []):
                print("   ", note)
        if common_gw:
            print(f"(gains over holding, over the common {common_gw}-GW window; a hit costs 4)")
    else:
        print("no source proposes a change and no candidate was given")
    summary["moves"] = table

    # 5. simulation ----------------------------------------------------------------------------
    sim = None
    if not a.no_sim:
        section("5. LEAGUE SIMULATION")
        f = sources[decider]
        cmd = ["run_simulation.py", "--data", a.data, "--me", a.me, "--my-squad", a.squad, "--my-bank", f"{a.bank:.1f}",
               "--k-by", "auto", "--proj", f]
        if a.names:
            cmd += ["--names", a.names]
        if a.committed:
            cmd += ["--committed", a.committed]
        # candidate squad files: the ones given, plus one per proposed move that passed or came close
        tmp_files, legend = [], []
        for row in table:
            key = next(k for k, mv in moves.items() if mv["label"] == row["label"])
            if key in cand_files:
                cmd += ["--candidate", cand_files[key]]; continue
            if any(v is None for v in row["gains"].values()):
                continue
            mv = moves[key]
            out_ids = {i for i in squad_ids if data.label(i) in mv["out"]}
            in_ids = []
            for lab in mv["in"]:
                el = next((e for e, p in data.players.items() if data.label(e) == lab), None)
                if el: in_ids.append(el)
            if len(in_ids) != len(mv["in"]):
                continue
            lines = []
            for ln in Path(a.squad).read_text(encoding="utf-8").splitlines():
                name = ln.split("#", 1)[0].split("@", 1)[0].strip()
                if name and name in mv["out"]:
                    continue
                lines.append(ln)
            lines += [data.label(e) for e in in_ids]
            tf = Path(f"cand{len(tmp_files) + 1}.local.txt"); tf.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp_files.append(tf); legend.append(f"{tf.stem} = {row['label']}"); cmd += ["--candidate", str(tf)]
        rc, out, secs = run(cmd, timeout=3600)
        sim = parse_simulation(out)
        if sim["error"]:
            failures.append("run_simulation"); print("! simulation failed:\n" + sim["error"])
        else:
            fc = sim["forecast"].get("tiered") or sim["forecast"].get("ignored")
            print(f"{decider}: P(1st) {fc['p1']:.0f}%, top 3 {fc['top3']:.0f}%, any prize {fc['money']:.0f}%, EV ${fc['ev']:.0f} (skill tiered; window {fc['window']:.1f})")
            for c in sim["candidates"]:
                print(f"  {c['name'][:40]:40} window {c['window']:.1f}  P(1st) {c['p1']:.0f}%  EV ${c['ev']:.0f} ({c['vs']:+.0f} vs yours)")
            for ln in legend:
                print("  " + ln)
            print(f"({secs:.0f}s)")
        summary["sim"] = sim

    # 6. checks --------------------------------------------------------------------------------
    section("6. CHECKS")
    watch_names = [x.strip() for x in a.watch.split(";") if x.strip()]
    for mv in moves.values():
        for i in mv["in"]:
            if i not in watch_names and i not in [data.label(e) for e in squad_ids]:
                watch_names.append(i)
    watch = ";".join(watch_names)
    rc, out, secs = run(["price_watch.py", "--data", a.data, "--squad", a.squad] + (["--watch", watch] if watch else []))
    moves_in_view = [ln for ln in out.splitlines() if re.search(r"\b(RISE|FALL)\b", ln)]
    first = next((ln for ln in out.splitlines() if ln.startswith("next price")), "")
    print("prices: " + (first or "(no header)"))
    print("\n".join("  " + ln for ln in moves_in_view) if moves_in_view else "  no price change in view for your 15 or the watched players")
    if rc: failures.append("price_watch"); print(tail(out, 3))
    rc, out, secs = run(["fixture_watch.py", "--data", a.data, "--squad", a.squad])
    print("fixtures: " + tail(out, 6).replace("\n", "\n  "))
    if rc: failures.append("fixture_watch")
    rc, out, secs = run(["starter_risk.py", "--data", a.data, "--squad", a.squad] + (["--watch", watch] if watch else []))
    flagged, seen = [], set()
    for ln in out.splitlines():
        if re.search(r"\b(OUT|DOUBT|watch)\b", ln) and not ln.startswith("player") and ln.strip() not in seen:
            seen.add(ln.strip()); flagged.append(ln)
    print("starters: " + ("\n  " + "\n  ".join(flagged) if flagged else "nobody flagged"))
    if rc: failures.append("starter_risk"); print(tail(out, 3))
    summary["prices"] = moves_in_view; summary["flags"] = flagged

    # 7. ledger ----------------------------------------------------------------------------------
    section("7. KEEPING SCORE")
    if a.ledger:
        rc, out, secs = run(["ledger.py", "score", "--data", a.data, "--ledger", a.ledger])
        print(tail(out, 8))
        summary["ledger"] = next((ln for ln in out.splitlines() if ln.startswith("scored")), tail(out, 1))
        if rc: failures.append("ledger score")
    else:
        print("no --ledger given")
    if a.frozen:
        rc, out, secs = run(["ledger.py", "benchmark", "--data", a.data, "--me", a.me, "--frozen", a.frozen, "--from", str(a.start)])
        print(tail(out, 8))
        summary["benchmark"] = tail(out, 1)
        if rc: failures.append("ledger benchmark")
    else:
        print("no --frozen given")

    # SUMMARY ---------------------------------------------------------------------------------
    section("SUMMARY")
    print(f"data: {summary['data']}")
    print(f"sources: {summary['sources']} — decisions on the {decider}")
    for src in sources:
        r0 = results[src][0]; rc_ = results[src]["ceiling"]
        if r0["hold"] is not None:
            print(f"  {src:10} hold {r0['hold']:6.1f}  ceiling {rc_['best'] if rc_['best'] is not None else float('nan'):6.1f}  ({windows[src][1]})")
    if sim and not sim.get("error"):
        fc = sim["forecast"].get("tiered") or sim["forecast"].get("ignored")
        print(f"odds ({decider}): P(1st) {fc['p1']:.0f}%, top 3 {fc['top3']:.0f}%, any prize {fc['money']:.0f}%, EV ${fc['ev']:.0f}")
    clears = [row for row in table if "CLEARS" in row.get("verdict", "")]
    print("decisions, ranked by the points at stake:")
    ranked = sorted(table, key=lambda row: -(row.get("net") if row.get("net") is not None else -99))
    for row in ranked:
        net = row.get("net")
        print(f"  {(('%+5.1f' % net) if net is not None else '    —')}  {row['label'][:80]}  → {row['verdict']}")
    print(f"  (points at stake = gain on the {decider} over the common window, net of hits)")
    if clears:
        print("ACTION candidates: " + "; ".join(row["label"] for row in clears) + " — confirm money, flags and the underlying numbers before recommending")
    else:
        print("NO-ACTION: nothing clears the bar; the saved team stands (check the XI, captain and bench order below)")
    xi = results[decider]["xi"]
    if xi["rows"]:
        starters = [row for row in xi["rows"] if row["st"] >= 1]
        bench = [row for row in xi["rows"] if row["st"] == 0]
        cap = next((row["label"] for row in xi["rows"] if row["cap"] >= 1), "?")
        gk_sub = [row for row in bench if row["pos"] == "GK"]
        outfield = sorted([row for row in bench if row["pos"] != "GK"], key=lambda row: -row["proj"])
        print(f"XI on the {decider} (next GW): " + ", ".join(f"{row['label']}{' (C)' if row['label'] == cap else ''}" for row in starters))
        print("bench order (by projection; a flagged player goes last before a blackout): " + ", ".join(f"{row['label']} {row['proj']:.1f}" for row in outfield) + (f"; GK {gk_sub[0]['label']}" if gk_sub else ""))
        caps = {src: results[src]["xi"]["rows"] and next((row["label"] for row in results[src]["xi"]["rows"] if row["cap"] >= 1), None) for src in sources}
        caps = {s: c for s, c in caps.items() if c}
        if len(set(caps.values())) > 1:
            print("! captain differs by source: " + ", ".join(f"{s} {c}" for s, c in caps.items()) + " — keep the current armband unless every source agrees")
        else:
            print(f"captain: {cap} on every source")
    if summary["flags"]:
        def short(ln):
            m = re.match(r"^(.+?\))\s", ln.strip()); who = m.group(1) if m else ln.split()[0]
            what = "OUT" if re.search(r"\sOUT\s", ln) else ("DOUBT" if "DOUBT" in ln else "watch")
            return f"{who} {what}"
        print("flags: " + " | ".join(short(ln) for ln in summary["flags"]))
    if summary["prices"]:
        print("prices: " + " | ".join(re.sub(r"\s+", " ", ln.strip()) for ln in summary["prices"]))
    if "ledger" in summary: print("ledger: " + summary["ledger"].replace("\n", " / "))
    if "benchmark" in summary: print("benchmark: " + summary["benchmark"].replace("\n", " / "))
    if failures:
        print("! steps that failed: " + ", ".join(failures) + " — say so in the message")
    print(f"done in {(dt.datetime.now(dt.timezone.utc) - started).total_seconds():.0f}s")
    if a.out:
        Path(a.out).write_text("".join(log), encoding="utf-8")
        print(f"report written to {a.out}")


if __name__ == "__main__":
    main()
