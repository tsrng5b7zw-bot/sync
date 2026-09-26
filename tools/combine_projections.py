#!/usr/bin/env python3
"""
Average two or more projection files (fplform dump, tools/house_projections.py, tools/
fetch_open_projections.py, tools/fetch_hgb_projections.py) player by player and week by week.

  python tools/combine_projections.py proj_open.txt proj_house.txt proj_hgb.txt --weights 2,1,1 [--out proj_blend.txt]
  python tools/combine_projections.py proj.txt proj_blend.txt --common --out proj_cons.txt     # fplform + blend, 50/50

Players are matched on (web_name, team). Only gameweeks every file covers are kept. A player missing
from one file takes the others' average, unless --common drops him (use it when one file is fplform,
which lists only players it rates, so a player found only in the other file has not been vetted).
A file with fplform's prob column is weighted by each player's chance of appearing, as the tools
do, because fplform's points are "if he appears"; the other models already price availability in. Independent models make different mistakes, so their average
is usually closer to the truth than either; averaged over GW4-5 of 2026/27 the open + house blend
ranked players better than either model alone, and ahead of FPL's own ep_next in both weeks. The house and
gradient-boosting models share their inputs, so weight the three files 2,1,1.
"""
import argparse
from pathlib import Path


def load(path):
    lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    head = lines[0].split("|")
    gws = [h for h in head if h[:2] == "gw" and h[2:].isdigit()]
    rows = {}
    for ln in lines[1:]:
        d = dict(zip(head, ln.split("|")))
        rows[(d["name"], d["team"])] = d
    return gws, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+"); ap.add_argument("--out", default="proj_blend.txt")
    ap.add_argument("--weights", help="comma-separated, one per file (default equal)")
    ap.add_argument("--common", action="store_true", help="keep only players present in every file")
    a = ap.parse_args()
    loaded = [load(f) for f in a.files]
    w = [float(x) for x in a.weights.split(",")] if a.weights else [1.0] * len(loaded)
    if len(w) != len(loaded):
        raise SystemExit("--weights needs one number per file")
    common = [g for g in loaded[0][0] if all(g in gws for gws, _ in loaded)]
    if not common:
        raise SystemExit("the files share no gameweek")
    keys = (set.intersection(*(set(rows) for _, rows in loaded)) if a.common
            else set().union(*(rows.keys() for _, rows in loaded)))
    head = ["name", "team", "pos", "cost"] + common + ["nextn", "sources"]
    out = ["|".join(head)]
    for k in sorted(keys):
        have = [(wi, rows[k]) for wi, (_, rows) in zip(w, loaded) if k in rows]
        tw = sum(wi for wi, _ in have)
        def val(r, g):
            x = float(r.get(g) or 0)
            p = r.get("prob")
            if p not in (None, ""):                      # fplform: points if he appears, times the odds he does
                x *= float(p)
            return x
        vals = [sum(wi * val(r, g) for wi, r in have) / tw for g in common]
        base = have[0][1]
        out.append("|".join([k[0], k[1], base.get("pos", ""), base.get("cost", "")] + [f"{v:.2f}" for v in vals]
                            + [f"{sum(vals):.1f}", str(len(have))]))
    Path(a.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{a.out}: {len(keys)} players averaged over {len(a.files)} files, {common[0]}-{common[-1]}")


if __name__ == "__main__":
    main()
