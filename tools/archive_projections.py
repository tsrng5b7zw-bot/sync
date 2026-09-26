#!/usr/bin/env python3
"""
Keep a dated copy of today's projection files so tools/backtest.py can score them after the
matches. Run it after build_dataset (and after the no-browser blend), before the deadline.

  python tools/archive_projections.py [--data data_pull] [--archive archive] [--force]

Copies proj.txt (fplform, built by build_dataset), proj_open.txt, proj_house.txt, proj_hgb.txt and
proj_blend.txt, whichever exist, into archive/gw<next>/. An existing copy is kept unless --force,
so the first read of the week stands (a later one is saved beside it with a timestamp). The
archive folder is ignored by the repo: fplform's data is copyrighted and never leaves this machine.
"""
import argparse, datetime as dt, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data

FILES = ("proj.txt", "proj_open.txt", "proj_house.txt", "proj_hgb.txt", "proj_blend.txt")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--archive", default="archive")
    ap.add_argument("--force", action="store_true", help="replace an existing copy")
    a = ap.parse_args()
    d = Data(a.data)
    nxt = d.nxt_gw or ((d.cur_gw or 0) + 1)
    folder = Path(a.archive) / f"gw{nxt}"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%MZ")
    saved, kept = [], []
    for fn in FILES:
        src = Path(fn)
        if not src.is_file():
            continue
        dst = folder / fn
        if dst.exists() and not a.force:
            shutil.copy2(src, folder / f"{src.stem}.{stamp}{src.suffix}")
            kept.append(fn)
        else:
            shutil.copy2(src, dst)
            saved.append(fn)
    if not saved and not kept:
        sys.exit("nothing to archive: no projection file in the current folder")
    print(f"{folder}: saved {', '.join(saved) or 'nothing new'}"
          + (f"; kept the earlier {', '.join(kept)} (today's copy saved with a timestamp)" if kept else ""))


if __name__ == "__main__":
    main()
