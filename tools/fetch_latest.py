#!/usr/bin/env python3
"""
Download the collector's latest data from GitHub into a local folder.

  python tools/fetch_latest.py <owner>/<repo> [--dest DIR] [--elements] [--prices N]

Reads raw.githubusercontent.com only (that host is reachable from the Claude workspace; the FPL site is not).
Default dest: ./data_pull (files land in latest/ and prices/), which is where the other tools look by default.
"""
import argparse, sys, urllib.request, urllib.error
from pathlib import Path

RAW = "https://raw.githubusercontent.com/{repo}/main/data/{path}"


def fetch(repo, path):
    url = RAW.format(repo=repo, path=path)
    req = urllib.request.Request(url, headers={"User-Agent": "fpl-fetch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", help="GitHub owner/repo of the collector, e.g. owner/repo")
    ap.add_argument("--dest", default="data_pull")
    ap.add_argument("--elements", action="store_true", help="also download per-player element JSON (big)")
    ap.add_argument("--prices", type=int, default=14, help="how many daily price snapshots to fetch")
    a = ap.parse_args()
    dest = Path(a.dest)
    try:
        manifest = fetch(a.repo, "latest/manifest.txt").decode().split()
    except urllib.error.HTTPError as e:
        sys.exit(f"cannot read manifest from {a.repo}: {e} — has the collector run at least once?")
    n = 0
    for rel in manifest:
        if rel.startswith("elements/") and not a.elements:
            continue
        if rel.startswith("live_gw") and rel.endswith(".json"):
            continue  # the CSV version is enough
        out = dest / "latest" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(fetch(a.repo, f"latest/{rel}"))
        n += 1
    try:
        idx = fetch(a.repo, "prices/index.txt").decode().split()
        for name in idx[-a.prices:]:
            out = dest / "prices" / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(fetch(a.repo, f"prices/{name}"))
            n += 1
    except urllib.error.HTTPError:
        pass
    meta = (dest / "latest" / "meta.json").read_text() if (dest / "latest" / "meta.json").exists() else ""
    print(f"fetched {n} files into {dest}/ ; meta: {meta}")


if __name__ == "__main__":
    main()
