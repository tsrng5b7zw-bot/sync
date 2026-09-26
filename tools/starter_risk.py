#!/usr/bin/env python3
"""
Will he play? One line per squad player (and any target): FPL's flag and news, the odds FPL gives,
his last three gameweeks' minutes, and what the market is doing, with a verdict.

  python tools/starter_risk.py [--data data_pull] --squad my_squad.local.txt [--watch "Name (TEAM);Name (TEAM)"]

Verdicts:
  OUT     flagged injured, suspended or unavailable, or FPL gives him 50% or less
  DOUBT   FPL gives him 75%, or he has news, or he did not start last time
  watch   nothing on him but the market: more than twice as many owners out as in this gameweek,
          on real volume, which sometimes runs ahead of the news
  ok      nothing against him
The projections already price in FPL's flag for the next week; this is the check that a starter
has not quietly become a substitute, which no projection catches until the minutes drop.
"""
import argparse, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, read_csv

RECENT = 3


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--squad", help="your 15, one 'web_name (TEAM)' per line")
    ap.add_argument("--watch", default="", help="'Name (TEAM);Name (TEAM)' to add")
    ap.add_argument("--all", action="store_true", help="every player with a flag or fresh news, not just yours")
    a = ap.parse_args()
    d = Data(a.data)
    key = {(p["web_name"], p["team"]): el for el, p in d.players.items()}
    names = []
    if a.squad:
        names += [ln.strip() for ln in Path(a.squad).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    names += [x.strip() for x in a.watch.split(";") if x.strip()]
    els = []
    for n in names:
        nm, _, tm = n.rpartition(" (")
        el = key.get((nm, tm.rstrip(")")))
        if el is None:
            print(f"! {n}: not found; write it as 'web_name (TEAM)'")
        else:
            els.append(el)
    last = d.last_finished_gw() or 0
    window = list(range(max(1, last - RECENT + 1), last + 1))
    mins = defaultdict(lambda: defaultdict(int))
    hp = d.D / "player_gw_history.csv"
    if hp.exists():
        for r in read_csv(hp):
            g = int(r["round"])
            if g in window:
                mins[int(r["id"])][g] += int(float(r["minutes"] or 0))
    if a.all:
        els += [el for el, p in d.players.items() if el not in els and float(p.get("selected_by_percent") or 0) >= 2
                and (p.get("status") != "a" or (p.get("news") or "").strip())]

    print(f"{'player':22}{'flag':>5}{'odds':>5}  {'GW' + '/'.join(str(g) for g in window):>12}  {'out:in':>7}  verdict  news")
    for el in els:
        p = d.players[el]
        status = p.get("status") or "a"
        chance = p.get("chance_of_playing_next_round")
        chance = None if chance in (None, "", "None") else int(float(chance))
        news = (p.get("news") or "").strip()
        m = [mins[el].get(g, 0) for g in window]
        t_in, t_out = float(p.get("transfers_in_event") or 0), float(p.get("transfers_out_event") or 0)
        ratio = f"{t_out / t_in:.1f}" if t_in >= 1000 else ("-" if t_out < 1000 else f"{t_out / 1000:.0f}k out")
        reasons = []
        if status in ("i", "s", "u", "n") or (chance is not None and chance <= 50):
            verdict = "OUT"
            reasons.append({"i": "injured", "s": "suspended", "u": "unavailable", "n": "not in squad"}.get(status, f"{chance}%"))
        else:
            verdict = "ok"
            if chance is not None and chance < 100:
                reasons.append(f"{chance}%")
            if news:
                reasons.append("news")
            if window and m and m[-1] < 60:
                reasons.append("did not start last time" if m[-1] == 0 else f"{m[-1]} min last time")
            if t_in >= 1000 and t_out > 2 * t_in and t_out >= 20000:
                reasons.append("owners leaving")
            if reasons:
                verdict = "watch" if reasons == ["owners leaving"] else "DOUBT"
        print(f"{p['web_name'] + ' (' + p['team'] + ')':22}{status:>5}{'' if chance is None else str(chance):>5}  "
              f"{'/'.join(str(x) for x in m):>12}  {ratio:>7}  {verdict:7}  {news[:60]}{' [' + ', '.join(reasons) + ']' if reasons and verdict != 'OUT' else ''}")


if __name__ == "__main__":
    main()
