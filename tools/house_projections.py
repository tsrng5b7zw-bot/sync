#!/usr/bin/env python3
"""
Projections built only from the FPL data the collector already pulls: the fallback for when no
browser can read fplform (a check-in running while your computer is off).

  python tools/house_projections.py [--data data_pull] [--weeks 6] [--out proj_house.txt]
  python tools/build_dataset.py --proj proj_house.txt       # then the other tools as usual

Per player:
  per-90 value   what his underlying numbers imply (xG, xA, xGC clean-sheet odds, defensive
                 contributions, saves, bonus, cards; see Data.underlying_per90), shrunk toward
                 the norm for his position and price, since five games are few;
  minutes        his share of 60-minute and substitute appearances over the last four gameweeks,
                 with FPL's injury and suspension flags applied to the next gameweek;
  fixtures       a multiplier by position, opponent difficulty and venue, measured on fplform's
                 own projections (September 2026), times the club's number of matches that week.

It is cruder than fplform: no bookmaker odds, set-piece news or line-up intelligence. Read it as a
second opinion and label decisions made on it. The file has fplform's layout, so every tool reads it.
"""
import argparse, csv, json, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, read_csv

# Points relative to a player's own average, by position, fixture difficulty (2 easy .. 5 hard) and
# venue; measured from fplform's GW6-11 projections for regular starters, September 2026.
FIXTURE = {
    "GK":  {(2, "H"): 1.05, (2, "A"): 0.98, (3, "H"): 1.04, (3, "A"): 0.96, (4, "H"): 1.01, (4, "A"): 0.95, (5, "H"): 0.98, (5, "A"): 0.96},
    "DEF": {(2, "H"): 1.08, (2, "A"): 0.99, (3, "H"): 1.07, (3, "A"): 0.94, (4, "H"): 1.02, (4, "A"): 0.91, (5, "H"): 0.95, (5, "A"): 0.91},
    "MID": {(2, "H"): 1.07, (2, "A"): 1.02, (3, "H"): 1.05, (3, "A"): 0.95, (4, "H"): 1.00, (4, "A"): 0.93, (5, "H"): 0.95, (5, "A"): 0.91},
    "FWD": {(2, "H"): 1.07, (2, "A"): 1.03, (3, "H"): 1.05, (3, "A"): 0.95, (4, "H"): 1.01, (4, "A"): 0.93, (5, "H"): 0.96, (5, "A"): 0.90},
}
# fplform's projections run lower than raw expected points (they price in rotation and regression);
# these put this model on the same scale, measured on the same September 2026 comparison
SCALE = {"GK": 0.90, "DEF": 0.92, "MID": 0.88, "FWD": 0.85}
PRIOR_GAMES = 4          # weight of the position-and-price norm, in full games
RECENT = 4               # gameweeks used for the minutes model
SUB_POINTS = 1.2         # a substitute appearance: the appearance point plus a little


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--out", default="proj_house.txt")
    ap.add_argument("--weeks", type=int, default=6, help="gameweeks to project from the next one")
    a = ap.parse_args()
    d = Data(a.data)
    nxt = d.nxt_gw or ((d.cur_gw or 0) + 1)
    gws = [g for g in range(nxt, nxt + a.weeks) if g <= 38]

    # fixtures: per club and gameweek, a list of (difficulty, venue)
    fx = defaultdict(list)
    for r in read_csv(d.D / "fixtures.csv"):
        if r.get("event") in (None, "", "None"):
            continue
        g = int(r["event"])
        fx[(r["team_h"], g)].append((int(r["team_h_difficulty"] or 3), "H"))
        fx[(r["team_a"], g)].append((int(r["team_a_difficulty"] or 3), "A"))

    # recent minutes from the per-player history (the last RECENT finished gameweeks)
    last = d.last_finished_gw() or 0
    window = set(range(max(1, last - RECENT + 1), last + 1))
    mins = defaultdict(lambda: defaultdict(int))
    hp = d.D / "player_gw_history.csv"
    if hp.exists():
        for r in read_csv(hp):
            g = int(r["round"])
            if g in window:
                mins[int(r["id"])][g] += int(float(r["minutes"] or 0))     # doubles add up

    # position-and-price norm: per-90 value regressed on price among regular players
    prior = {}
    for pos in ("GK", "DEF", "MID", "FWD"):
        pts = [(float(p["price"]), d.underlying_per90(el)) for el, p in d.players.items()
               if p["pos"] == pos and float(p["minutes"] or 0) >= 270]
        pts = [(x, y) for x, y in pts if y is not None]
        if len(pts) >= 5:
            mx, my = st.mean(x for x, _ in pts), st.mean(y for _, y in pts)
            sxx = sum((x - mx) ** 2 for x, _ in pts)
            b = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx else 0.0
            prior[pos] = (my - b * mx, b)
        else:
            prior[pos] = (3.0, 0.0)

    rows = []
    for el, p in d.players.items():
        pos, team, price = p["pos"], p["team"], float(p["price"])
        m = float(p["minutes"] or 0)
        a0, b0 = prior[pos]
        norm = a0 + b0 * price
        u = d.underlying_per90(el)
        per90 = norm if u is None else (m / 90 * u + PRIOR_GAMES * norm) / (m / 90 + PRIOR_GAMES)
        played = [mins[el].get(g, 0) for g in sorted(window)]
        n = max(1, len(window))
        p60 = sum(1 for x in played if x >= 60) / n
        psub = sum(1 for x in played if 0 < x < 60) / n
        long_mins = [x for x in played if x >= 60]
        share = min(1.0, (st.mean(long_mins) if long_mins else 75) / 90)
        status = p.get("status")
        chance = p.get("chance_of_playing_next_round")
        chance = None if chance in (None, "", "None") else float(chance) / 100
        if status == "u":
            p60 = psub = 0.0
        per_match = (p60 * per90 * share + psub * SUB_POINTS) * SCALE[pos]
        out = {}
        for g in gws:
            factor = 1.0
            if g == nxt:
                if status in ("i", "s", "n") and not chance:
                    factor = 0.0
                elif chance is not None:
                    factor = chance
            out[g] = round(sum(per_match * FIXTURE[pos].get(f, 1.0) for f in fx.get((team, g), [])) * factor, 2)
        ros = sum(sum(per_match * FIXTURE[pos].get(f, 1.0) for f in fx.get((team, g), []))
                  for g in range(nxt, 39))
        if sum(out.values()) <= 0 and price < 5.5:
            continue                                     # nobody who matters
        rows.append((p["web_name"], team, pos, price, out, ros, p))

    head = ["name", "team", "pos", "cost", "merit", "form"] + [f"gw{g}" for g in gws] + ["nextn", "ros", "sofar", "chance", "avail", "sel", "news"]
    lines = ["|".join(head)]
    for name, team, pos, price, out, ros, p in sorted(rows, key=lambda r: -sum(r[4].values())):
        lines.append("|".join(str(x) for x in [name, team, pos, price, "", p.get("form", "")]
                              + [f"{out[g]:.2f}" for g in gws]
                              + [f"{sum(out.values()):.1f}", f"{ros:.0f}", p.get("total_points", ""),
                                 p.get("chance_of_playing_next_round") or "", p.get("status", ""),
                                 p.get("selected_by_percent", ""), (p.get("news") or "").replace("|", "/")[:80]]))
    Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{a.out}: {len(rows)} players, GW{gws[0]}-GW{gws[-1]}, from the FPL data pulled "
          f"{d.meta.get('pulled_at_utc')} (no browser needed). Label decisions made on it.")


if __name__ == "__main__":
    main()
