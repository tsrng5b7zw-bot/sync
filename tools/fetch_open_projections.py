#!/usr/bin/env python3
"""
Fetch the latest projections published by an open-source FPL model on GitHub, for when no browser
can read fplform (a check-in running while your computer is off).

  python tools/fetch_open_projections.py [--data data_pull] [--out proj_open.txt]

Source: github.com/blueladd11-commits-tocode/fpl-projections. It publishes, before every deadline,
six gameweeks of expected points for every player, timestamped and scored afterwards against the
real results (its out/scorecard_gw*.json). In 2026/27 it has ranked players better than FPL's own
ep_next every week so far. Its files are read here at run time and never copied into this repo.

The output has the same layout as the fplform dump, so every tool reads it; combine it with
tools/house_projections.py output using tools/combine_projections.py.
"""
import argparse, csv, datetime as dt, json, re, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data
from fetch_hgb_projections import spearman

REPO = "https://github.com/blueladd11-commits-tocode/fpl-projections"
MAX_BYTES = 20_000_000
MAX_POINTS = 25.0       # per gameweek; a double gameweek stays well under this
MIN_AGREEMENT = 0.3     # rank correlation with FPL's ep_next: honest models sit at 0.45-0.95, scrambled data near 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--out", default="proj_open.txt")
    a = ap.parse_args()
    d = Data(a.data)
    nxt = d.nxt_gw or ((d.cur_gw or 0) + 1)
    with tempfile.TemporaryDirectory() as tmp:
        git = ["git", "-c", "core.symlinks=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never"]
        run = lambda *c: subprocess.run(git + list(c), cwd=tmp, check=True, capture_output=True, text=True, timeout=180)
        try:
            run("clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse", "--no-recurse-submodules", REPO, "src")
            run("-C", "src", "sparse-checkout", "set", "--no-cone", "out/projections_gw*.csv", "out/projections_gw*.meta.json")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            sys.exit(f"could not fetch {REPO}: {str(getattr(e, 'stderr', '') or e).strip()[:200]}")
        files = sorted(f for f in Path(tmp, "src", "out").glob("projections_gw*.csv")
                       if f.is_file() and not f.is_symlink() and f.stat().st_size < MAX_BYTES)
        pat = re.compile(r"projections_gw(\d+)_(\d{8}T\d{6}Z)")
        found = [(int(m.group(1)), m.group(2), f) for f in files if (m := pat.search(f.name))]
        if not found:
            sys.exit("no projection files found in the open repo")
        # prefer the newest file for the next gameweek; otherwise the newest file of any gameweek,
        # whose six-week horizon still covers the next one
        pick = max((x for x in found if x[0] == nxt), default=None, key=lambda x: x[1]) or max(found, key=lambda x: (x[0], x[1]))
        gw0, stamp, path = pick
        meta_p = path.with_suffix(".meta.json")
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() and not meta_p.is_symlink() else {}
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
    horizon = len(rows[0]["xp_next"].split(";")) if rows else 0
    gws = list(range(gw0, gw0 + horizon))
    keep = [g for g in gws if g >= nxt]
    if not keep:
        sys.exit(f"the newest open projections cover GW{gws[0]}-GW{gws[-1]}, all before GW{nxt}")
    head = ["name", "team", "pos", "cost"] + [f"gw{g}" for g in keep] + ["nextn", "p_start", "xmins", "fpl_ep_next"]
    out = ["|".join(head)]
    ours, theirs = [], []
    for r in rows:
        try:
            v = dict(zip(gws, (float(x) for x in r["xp_next"].split(";"))))
        except ValueError:
            sys.exit(f"non-numeric projection for {r.get('web_name')}; refusing the file")
        vals = [v[g] for g in keep]
        if any(not (0.0 <= x <= MAX_POINTS) for x in vals):
            sys.exit(f"projection out of range for {r.get('web_name')} ({vals}); refusing the file")
        p = d.players.get(int(r["element"])) if str(r.get("element", "")).isdigit() else None
        if p is not None and p.get("ep_next") not in (None, "") and keep[0] == nxt:
            ours.append(float(p["ep_next"])); theirs.append(vals[0])
        out.append("|".join([r["web_name"], r["team"], r["pos"], r["price"]] + [f"{x:.2f}" for x in vals]
                            + [f"{sum(vals):.1f}", r.get("p_start", ""), r.get("xmins", ""), r.get("fpl_ep_next", "")]))
    if len(ours) > 50:
        agree = spearman(theirs, ours)
        if agree < MIN_AGREEMENT:
            sys.exit(f"GW{nxt} projections barely track FPL's ep_next (rank correlation {agree:.2f}); refusing the file")
    Path(a.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    when = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    age_h = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600
    note = "" if gw0 == nxt else f" (no GW{nxt} file yet: it appears about 72 hours before that deadline)"
    print(f"{a.out}: {len(rows)} players, GW{keep[0]}-GW{keep[-1]}, from the open model's GW{gw0} projection made "
          f"{when:%d %b %H:%M} UTC, {age_h:.0f}h ago, model {meta.get('model_version', '?')}{note}")


if __name__ == "__main__":
    main()
