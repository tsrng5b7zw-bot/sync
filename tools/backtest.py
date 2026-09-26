#!/usr/bin/env python3
"""
Score every projection source against the points actually scored, on the same players and the
same test, week by week and over the multi-week windows the decisions are made on.

  python tools/backtest.py [--data data_pull] [--gws 1-5] [--archive archive] [--ci]

Sources and where their past projections come from:
  open      github.com/blueladd11-commits-tocode/fpl-projections keeps every dated file it published;
            for each week the newest file made before that deadline is used, so nothing is scored
            with hindsight
  ep_next   FPL's own number for the week, as recorded in those same files
  hgb       github.com/cooperoliver-py/FPL-Core-Insights keeps predictions/archive/<season>/GWnn.csv
  house     tools/house_projections.py --as-of GW, rebuilt from the per-gameweek history
  fplform   only from archive/gwN/proj.txt saved by tools/archive_projections.py on the day; it
            cannot be fetched after the fact, and its data never goes in the repo
  blend     2 x open + house + hgb, the no-browser mix, where all three exist
An archived proj_open.txt / proj_hgb.txt / proj_house.txt is preferred over a refetch or rebuild.

Two tests. "Next week": each source's projection for the coming week, made before its deadline.
"Window": one source file per starting week, its projections for that week and the following
ones summed and scored against the points those weeks produced, exactly as the optimiser uses
them; later weeks in the window are harder, and a source that leans on last week's points (FPL's
ep_next, and partly the house model) gets no credit here for points it had already seen.

The GitHub files are parsed as text only, never run, and are read into a temporary directory.
Spearman near 0.4 for a single week is what public models achieve; longer windows score higher.
"""
import argparse, csv, datetime as dt, json, random, re, subprocess, sys, tempfile
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, read_csv
from fetch_hgb_projections import spearman

OPEN_REPO = "https://github.com/blueladd11-commits-tocode/fpl-projections"
HGB_REPO = "https://github.com/cooperoliver-py/FPL-Core-Insights"
MAX_BYTES = 20_000_000
GIT = ["git", "-c", "core.symlinks=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never"]
SOURCES = ("fplform", "open", "house", "hgb", "blend", "ep_next")


def clone(repo, patterns, tmp, name):
    run = lambda *c: subprocess.run(GIT + list(c), cwd=tmp, check=True, capture_output=True, text=True, timeout=300)
    run("clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse", "--no-recurse-submodules", repo, name)
    run("-C", name, "sparse-checkout", "set", "--no-cone", *patterns)
    return Path(tmp, name)


def safe(p):
    return p.is_file() and not p.is_symlink() and p.stat().st_size < MAX_BYTES


def rows_of(path, delimiter=","):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=delimiter))


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


class Store:
    """proj[source][(made_for, week)] = {element: points}: the projection for `week` from the file
    made on the eve of `made_for`."""

    def __init__(self):
        self.proj = defaultdict(dict)

    def put(self, src, g0, g, d):
        if d:
            self.proj[src][(g0, g)] = d

    def has(self, src, g0):
        return any(k[0] == g0 for k in self.proj[src])


def load_open(out_dir, deadlines, gws, store):
    pat = re.compile(r"projections_gw(\d+)_(\d{8}T\d{6}Z)")
    files = []
    for f in sorted(out_dir.glob("projections_gw*.csv")):
        m = pat.search(f.name)
        if m and safe(f):
            files.append((int(m.group(1)), dt.datetime.strptime(m.group(2), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc), f))
    for g0 in gws:
        if store.has("open", g0):
            continue
        cands = [(gw0, t, f) for gw0, t, f in files if gw0 == g0 and t < deadlines[g0]]
        if not cands:
            continue
        _, _, f = max(cands, key=lambda x: x[1])
        per_week, ep = defaultdict(dict), {}
        for r in rows_of(f):
            el = num(r.get("element"))
            if el is None:
                continue
            el = int(el)
            for k, x in enumerate((r.get("xp_next") or "").split(";")):
                v = num(x)
                if v is not None:
                    per_week[g0 + k][el] = v
            v = num(r.get("fpl_ep_next"))
            if v is not None:
                ep[el] = v
        for g, d in per_week.items():
            store.put("open", g0, g, d)
        store.put("ep_next", g0, g0, ep)


def load_hgb(files, deadlines, gws, store):
    by_gw = {}
    for f in files:
        m = re.search(r"GW(\d+)\.csv$", f.name)
        if m and safe(f):
            by_gw[int(m.group(1))] = f
    for g0 in gws:
        if store.has("hgb", g0) or g0 not in by_gw:
            continue
        rows = rows_of(by_gw[g0])
        made = rows[0].get("forecast_created_at") if rows else None
        try:
            when = dt.datetime.fromisoformat(made.replace("Z", "+00:00")) if made else None
        except ValueError:
            when = None
        if when and when >= deadlines[g0]:
            continue                                # made after the deadline: hindsight
        per_week = defaultdict(dict)
        for r in rows:
            el = num(r.get("player_id"))
            if el is None:
                continue
            for k, v in r.items():
                m = re.fullmatch(r"GW(\d+)_predicted_points", k or "")
                x = num(v)
                if m and x is not None:
                    per_week[int(m.group(1))][int(el)] = x
        for g, d in per_week.items():
            store.put("hgb", g0, g, d)


def load_txt(path, key, prob_weight=False):
    """A file in the tools' pipe-separated layout -> {week: {element: points}}."""
    per_week = defaultdict(dict)
    for r in rows_of(path, "|"):
        el = key.get((r.get("name"), r.get("team")))
        if el is None:
            continue
        p = num(r.get("prob")) if prob_weight else None
        for k, v in r.items():
            m = re.fullmatch(r"gw(\d+)", k or "")
            x = num(v)
            if m and x is not None:
                per_week[int(m.group(1))][el] = x * (p if p is not None and p < 0.5 else 1.0)
    return per_week


def house_asof(data_root, g0, key):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "h.txt")
        subprocess.run([sys.executable, str(Path(__file__).with_name("house_projections.py")), "--data", data_root,
                        "--as-of", str(g0), "--out", str(out)], check=True, capture_output=True, text=True, timeout=300)
        return load_txt(out, key)


def boot_ci(x, y, n=1000, seed=1):
    rnd = random.Random(seed); m = len(x); out = []
    for _ in range(n):
        idx = [rnd.randrange(m) for _ in range(m)]
        out.append(spearman([x[i] for i in idx], [y[i] for i in idx]))
    out.sort()
    return out[int(0.05 * n)], out[int(0.95 * n)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--gws", help="e.g. 2-5 (default: every finished gameweek from 2)")
    ap.add_argument("--archive", default="archive", help="folder of archive/gwN/proj_*.txt saved on the day")
    ap.add_argument("--top", type=int, default=30, help="size of the top-picks test")
    ap.add_argument("--window", type=int, default=5, help="weeks in the window test")
    ap.add_argument("--ci", action="store_true", help="90%% bootstrap intervals on the next-week test (slower)")
    ap.add_argument("--no-fetch", action="store_true", help="use only archived files, no GitHub")
    a = ap.parse_args()
    d = Data(a.data)
    last = d.last_finished_gw() or 0
    if a.gws:
        lo, _, hi = a.gws.partition("-")
        gws = list(range(int(lo), int(hi or lo) + 1))
    else:
        gws = list(range(2, last + 1))               # GW1 has no history for the house model
    gws = [g for g in gws if 1 <= g <= last]
    if not gws:
        sys.exit("no finished gameweek to score")
    boot = json.loads((d.D / "bootstrap.json").read_text(encoding="utf-8"))
    deadlines = {e["id"]: dt.datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00")) for e in boot["events"]}
    key = {(p["web_name"], p["team"]): el for el, p in d.players.items()}
    act, mins = defaultdict(float), defaultdict(int)
    for r in read_csv(d.D / "player_gw_history.csv"):
        act[(int(r["id"]), int(r["round"]))] += float(r["total_points"] or 0)
        mins[(int(r["id"]), int(r["round"]))] += int(float(r["minutes"] or 0))

    st = Store()
    arch = Path(a.archive)
    for g0 in gws:
        for src, fn, pw in (("fplform", "proj.txt", True), ("open", "proj_open.txt", False),
                            ("hgb", "proj_hgb.txt", False), ("house", "proj_house.txt", False)):
            f = arch / f"gw{g0}" / fn
            if f.is_file():
                for g, dd in load_txt(f, key, pw).items():
                    st.put(src, g0, g, dd)
    if not a.no_fetch:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                od = clone(OPEN_REPO, ["out/projections_gw*.csv"], tmp, "open")
                load_open(od / "out", deadlines, gws, st)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
                print(f"! open model history not fetched: {str(getattr(ex, 'stderr', '') or ex).strip()[:120]}")
            try:
                hd = clone(HGB_REPO, ["predictions/archive/*/GW*.csv"], tmp, "hgb")
                load_hgb(sorted(hd.glob("predictions/archive/*/GW*.csv")), deadlines, gws, st)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
                print(f"! gradient-boosting history not fetched: {str(getattr(ex, 'stderr', '') or ex).strip()[:120]}")
    for g0 in gws:
        if g0 >= 2 and not st.has("house", g0):
            try:
                for g, dd in house_asof(a.data, g0, key).items():
                    st.put("house", g0, g, dd)
            except subprocess.CalledProcessError as ex:
                print(f"! house model could not be rebuilt for GW{g0}: {ex.stderr.strip()[:120]}")
    for (g0, g) in list(st.proj["open"]):
        parts = [st.proj[s].get((g0, g)) for s in ("open", "house", "hgb")]
        if all(parts):
            ids = set(parts[0]) & set(parts[1]) & set(parts[2])
            st.put("blend", g0, g, {el: (2 * parts[0][el] + parts[1][el] + parts[2][el]) / 4 for el in ids})

    order = [s for s in SOURCES if st.proj.get(s)]
    if not order:
        sys.exit("nothing to score")
    P = st.proj
    played = lambda el, g: mins.get((el, g), 0) > 0

    print(f"NEXT WEEK: players who played, ranked by the projection made before that deadline (Spearman); "
          f"'top' = mean points of the source's {a.top} highest-ranked; (all who played){'; 90% interval' if a.ci else ''}\n")
    w = 22 if a.ci else 13
    print(f"{'week':6}{'n':>5}{'':7}" + "".join(f"{s:>{w}}" for s in order))
    for g in gws:
        here = [s for s in order if (g, g) in P[s]]
        pool = [el for el in d.players if played(el, g) and all(el in P[s][(g, g)] for s in here)]
        if len(pool) < 30:
            continue
        y = [act[(el, g)] for el in pool]
        cells = []
        for s in order:
            if s not in here:
                cells.append(f"{'-':>{w}}")
                continue
            x = [P[s][(g, g)][el] for el in pool]
            top = sorted(pool, key=lambda el: -P[s][(g, g)][el])[:a.top]
            tm = sum(act[(el, g)] for el in top) / len(top)
            cell = f"{spearman(x, y):.2f} top {tm:.1f}"
            if a.ci:
                lo, hi = boot_ci(x, y)
                cell = f"{spearman(x, y):.2f} [{lo:.2f},{hi:.2f}] {tm:.1f}"
            cells.append(f"{cell:>{w}}")
        print(f"GW{g:<4}{len(pool):5d} ({sum(y) / len(y):.1f})" + "".join(cells))

    print(f"\nWINDOW: one file per starting week, its projections for the next {a.window} weeks summed (as the "
          f"optimiser uses them) against the points those weeks produced; players who played every week")
    win_sources = [s for s in order if s != "ep_next"]
    print(f"{'window':12}{'n':>5}  " + "".join(f"{s:>13}" for s in win_sources))
    for g0 in gws:
        weeks = [g for g in range(g0, g0 + a.window) if g <= last]
        if len(weeks) < 2:
            continue
        here = [s for s in win_sources if all((g0, g) in P[s] for g in weeks)]
        if not here:
            continue
        pool = [el for el in d.players if all(played(el, g) for g in weeks) and all(el in P[s][(g0, g)] for s in here for g in weeks)]
        if len(pool) < 30:
            continue
        y = [sum(act[(el, g)] for g in weeks) for el in pool]
        cells = []
        for s in win_sources:
            if s not in here:
                cells.append(f"{'-':>13}")
                continue
            x = [sum(P[s][(g0, g)][el] for g in weeks) for el in pool]
            top = sorted(pool, key=lambda el: -sum(P[s][(g0, g)][el] for g in weeks))[:a.top]
            tm = sum(sum(act[(el, g)] for g in weeks) for el in top) / len(top) / len(weeks)
            cells.append(f"{spearman(x, y):.2f} top {tm:.1f}".rjust(13))
        print(f"GW{g0}-{weeks[-1]:<7}{len(pool):5d}  " + "".join(cells))
    if "fplform" not in order:
        print("\nfplform: no archived projection yet. Run tools/archive_projections.py after each browser read; "
              "it is scored once those weeks are played.")


if __name__ == "__main__":
    main()
