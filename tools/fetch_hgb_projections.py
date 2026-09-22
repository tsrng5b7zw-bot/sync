#!/usr/bin/env python3
"""
Fetch a second open-source model's projections: a gradient-boosting model published on GitHub and
rebuilt three times a day, for the no-browser blend.

  python tools/fetch_hgb_projections.py [--data data_pull] [--out proj_hgb.txt]

Source: github.com/cooperoliver-py/FPL-Core-Insights (predictions/latest.csv), a fork of the FPL Core
Insights dataset that adds a model. It freezes a forecast before every deadline and scores it
afterwards (predictions/performance.csv). On GW3-5 of 2026/27 it ranked players about as well as
the open model and better than FPL's own ep_next. It is built from the same FPL data as
tools/house_projections.py and agrees closely with it, so blend it at half the open model's weight:

  python tools/combine_projections.py proj_open.txt proj_house.txt proj_hgb.txt --weights 2,1,1

Only the one CSV is checked out and it is only parsed as numbers: nothing from the repo is run, and
its data is read at run time and never copied into this repo. A file that is stale, malformed or
out of line with FPL's own expected points is refused, so the blend falls back to the others.
"""
import argparse, csv, datetime as dt, math, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data

REPO = "https://github.com/cooperoliver-py/FPL-Core-Insights"
FILE = "predictions/latest.csv"
MAX_BYTES = 20_000_000
MAX_AGE_H = 72          # the model runs at 00:30, 08:30 and 16:30 UTC
MIN_PLAYERS = 400
MAX_POINTS = 25.0       # per gameweek; a double gameweek stays well under this
MIN_AGREEMENT = 0.3     # rank correlation with FPL's ep_next: honest models sit at 0.45-0.95, scrambled data near 0


def spearman(a, b):
    def ranks(v):
        order = sorted(range(len(v)), key=v.__getitem__); r = [0.0] * len(v); i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b); n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--out", default="proj_hgb.txt")
    a = ap.parse_args()
    d = Data(a.data)
    nxt = d.nxt_gw or ((d.cur_gw or 0) + 1)
    with tempfile.TemporaryDirectory() as tmp:
        git = ["git", "-c", "core.symlinks=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never"]
        run = lambda *c: subprocess.run(git + list(c), cwd=tmp, check=True, capture_output=True, text=True, timeout=180)
        try:
            run("clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse", "--no-recurse-submodules", REPO, "src")
            run("-C", "src", "sparse-checkout", "set", "--no-cone", FILE)
            sha = run("-C", "src", "rev-parse", "--short", "HEAD").stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            sys.exit(f"could not fetch {REPO}: {str(getattr(e, 'stderr', '') or e).strip()[:200]}")
        path = Path(tmp, "src", FILE)
        if path.is_symlink():
            sys.exit(f"{FILE} in {REPO} is a symlink, not data; refusing it")
        if not path.is_file():
            sys.exit(f"{FILE} is missing from {REPO}")
        if path.stat().st_size > MAX_BYTES:
            sys.exit(f"{FILE} is implausibly large ({path.stat().st_size} bytes); refusing it")
        rows = list(csv.DictReader(open(path, encoding="utf-8", newline="")))
    if len(rows) < MIN_PLAYERS:
        sys.exit(f"only {len(rows)} players in {FILE}; refusing it")
    created = rows[0].get("forecast_created_at", "")
    try:
        when = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        sys.exit(f"no readable forecast_created_at in {FILE}; refusing it")
    age_h = (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 3600
    if age_h > MAX_AGE_H:
        sys.exit(f"{FILE} was made {age_h:.0f}h ago ({when:%d %b %H:%M} UTC): the model has stopped updating; refusing it")
    gws = sorted(int(k[2:].split("_")[0]) for k in rows[0] if k.startswith("GW") and k.endswith("_predicted_points"))
    keep = [g for g in gws if g >= nxt]
    if not keep or keep[0] != nxt:
        sys.exit(f"{FILE} covers GW{gws[0]}-GW{gws[-1]} but the next gameweek is GW{nxt}; refusing it")
    out, ours, theirs = [], [], []
    for r in rows:
        el = r.get("player_id", "")
        p = d.players.get(int(el)) if el.isdigit() else None
        if p is None:
            continue
        try:
            vals = [float(r[f"GW{g}_predicted_points"] or 0) for g in keep]
        except ValueError:
            sys.exit(f"non-numeric projection for player {el}; refusing the file")
        if any(not (0.0 <= v <= MAX_POINTS) for v in vals):
            sys.exit(f"projection out of range for player {el} ({vals}); refusing the file")
        out.append("|".join([p["web_name"], p["team"], p["pos"], f"{int(p['now_cost']) / 10:.1f}"]
                            + [f"{v:.2f}" for v in vals] + [f"{sum(vals):.1f}", r.get(f"GW{nxt}_availability", "")]))
        ep = p.get("ep_next")
        if ep not in (None, ""):
            ours.append(float(ep)); theirs.append(vals[0])
    if len(out) < MIN_PLAYERS:
        sys.exit(f"only {len(out)} players matched the FPL data; refusing the file")
    agree = spearman(theirs, ours) if len(ours) > 50 else 1.0
    if agree < MIN_AGREEMENT:
        sys.exit(f"GW{nxt} projections barely track FPL's ep_next (rank correlation {agree:.2f}); refusing the file")
    head = ["name", "team", "pos", "cost"] + [f"gw{g}" for g in keep] + ["nextn", "avail"]
    Path(a.out).write_text("\n".join(["|".join(head)] + out) + "\n", encoding="utf-8")
    print(f"{a.out}: {len(out)} players, GW{keep[0]}-GW{keep[-1]}, from the gradient-boosting model's forecast made "
          f"{when:%d %b %H:%M} UTC, {age_h:.0f}h ago (commit {sha}); rank agreement with FPL's ep_next {agree:.2f}")


if __name__ == "__main__":
    main()
