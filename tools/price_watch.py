#!/usr/bin/env python3
"""
Price-change watch from FPL's own predictor (new in 2026/27), which the hourly pull already saves in
data/latest/bootstrap.json. No outside source is involved.

  python tools/price_watch.py --squad my_squad.local.txt [--watch "Name (TEAM);Name (TEAM)"] [--data data_pull]

For each player: progress towards the next rise (+) or fall (-) in percent, FPL's projection of that
figure at each of the next three price updates, and when he is first expected to cross 100%, which
is when FPL changes the price. Prices change at midnight UK time (6 pm Chicago; 7 pm on 25-31 Oct
and 14-27 Mar). FPL calls these figures a guide: late transfers can still move them.
"""
import argparse, datetime as dt, json, sys
from pathlib import Path
from zoneinfo import ZoneInfo

CHI = ZoneInfo("America/Chicago")


def names(spec):
    out = []
    for part in (spec or "").replace("\n", ";").split(";"):
        part = part.strip()
        if part and not part.startswith("#") and " (" in part:
            n, t = part.rsplit(" (", 1)
            out.append((n.strip(), t.rstrip(")").strip()))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--squad"); ap.add_argument("--watch")
    a = ap.parse_args()
    boot = json.loads(Path(a.data, "latest", "bootstrap.json").read_text(encoding="utf-8"))
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    by = {(e["web_name"], teams.get(e["team"], "")): e for e in boot["elements"]}
    stamps = (boot.get("game_config", {}).get("settings", {}) or {}).get("price_change_deadlines") or []
    when = [dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps]
    want = names(Path(a.squad).read_text(encoding="utf-8") if a.squad else "") + names(a.watch)
    if not want:
        sys.exit("give --squad and/or --watch")
    if "price_change_percent" not in boot["elements"][0]:
        sys.exit("this bootstrap.json has no price-change predictor fields")
    now = dt.datetime.now(dt.timezone.utc)
    upd = ", ".join(f"{w.astimezone(CHI):%a %d %b %-I %p}" for w in when) or "unknown"
    print(f"next price updates (Chicago): {upd}")
    print(f"{'player':24} {'price':>5}  {'now':>6}  {'next three updates':>22}   expected")
    for k in want:
        e = by.get(k)
        if not e:
            print(f"{k[0] + ' (' + k[1] + ')':24} not found"); continue
        if e.get("price_change_calibrating"):
            print(f"{k[0] + ' (' + k[1] + ')':24} FPL is still calibrating this player"); continue
        pct = float(e.get("price_change_percent") or 0)
        proj = sorted(e.get("price_change_projections") or [], key=lambda p: p.get("offset", 0))
        vals = [float(p.get("projected_percent") or 0) for p in proj]
        verdict = "no change in view"
        for i, v in enumerate(vals):
            if abs(v) >= 100:
                t = f"{when[i].astimezone(CHI):%a %-I %p}" if i < len(when) else f"update {i + 1}"
                verdict = f"{'RISE' if v > 0 else 'FALL'} {t}"; break
        lock = e.get("price_change_locked_until")
        if lock:
            lt = dt.datetime.fromisoformat(lock.replace("Z", "+00:00"))
            if lt > now:
                verdict += f" (locked until {lt.astimezone(CHI):%a %d %b})"
        print(f"{k[0] + ' (' + k[1] + ')':24} {e['now_cost'] / 10:5.1f}  {pct:+6.1f}  "
              f"{' '.join(f'{v:+7.1f}' for v in vals):>22}   {verdict}")


if __name__ == "__main__":
    main()
