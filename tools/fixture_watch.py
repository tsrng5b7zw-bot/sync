#!/usr/bin/env python3
"""
Watch the fixture list for blank and double gameweeks, the weeks that decide chip timing.

  python tools/fixture_watch.py [--data data_pull] [--squad my_squad.local.txt] [--from GW]

Reads data_pull/latest/fixtures.json (pulled hourly). A team with no fixture in a gameweek is a
blank; two or more is a double. Postponed matches show up first as fixtures with no gameweek at
all, so those are listed too: they become doubles later. With --squad, each flagged week shows how
many of your 15 play. Nothing unusual prints one line, so the check is safe to run every time.
"""
import argparse, json
from collections import defaultdict
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull")
    ap.add_argument("--squad", help="your 15, one 'web_name (TEAM)' per line")
    ap.add_argument("--from", dest="start", type=int, help="first gameweek to report (default: the next one)")
    a = ap.parse_args()
    latest = Path(a.data) / "latest"
    fixtures = json.loads((latest / "fixtures.json").read_text(encoding="utf-8"))
    boot = json.loads((latest / "bootstrap.json").read_text(encoding="utf-8"))
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    nxt = next((e["id"] for e in boot["events"] if e.get("is_next")), None) or 1
    start = a.start or nxt

    count = defaultdict(int)
    for f in fixtures:
        if f.get("event"):
            count[(f["event"], f["team_h"])] += 1
            count[(f["event"], f["team_a"])] += 1
    unscheduled = [f for f in fixtures if not f.get("event")]

    squad_teams = defaultdict(int)
    if a.squad:
        for ln in Path(a.squad).read_text(encoding="utf-8").splitlines():
            if ln.strip() and not ln.startswith("#"):
                squad_teams[ln.strip().rsplit("(", 1)[1].rstrip(")")] += 1

    flagged = False
    for gw in range(start, 39):
        blanks = sorted(short[t] for t in short if count.get((gw, t), 0) == 0)
        doubles = sorted(short[t] for t in short if count.get((gw, t), 0) >= 2)
        if not blanks and not doubles:
            continue
        flagged = True
        line = f"GW{gw}:"
        if blanks:
            line += f" BLANK for {', '.join(blanks)}"
        if doubles:
            line += f" DOUBLE for {', '.join(doubles)}"
        if a.squad:
            playing = sum(n for t, n in squad_teams.items() if t not in blanks)
            twice = sum(n for t, n in squad_teams.items() if t in doubles)
            line += f"  | of your 15: {playing} play, {twice} play twice"
        print(line)
    if unscheduled:
        flagged = True
        print(f"{len(unscheduled)} postponed fixture(s) not yet given a gameweek (future doubles):")
        for f in unscheduled:
            print(f"  {short[f['team_h']]} v {short[f['team_a']]}")
    if not flagged:
        print(f"fixtures GW{start}-38: every team plays once a week; no blank or double in view")


if __name__ == "__main__":
    main()
