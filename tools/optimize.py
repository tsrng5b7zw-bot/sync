#!/usr/bin/env python3
"""
Squad optimiser: the 15 that maximise projected points over the projection window, with a legal
XI and captain chosen for every gameweek, under budget and the 3-per-club cap.

  python tools/optimize.py --squad my_squad.local.txt --bank 0.6 --me <alias> --transfers 1   # best single move
  python tools/optimize.py --squad my_squad.local.txt --bank 0.6 --me <alias> --transfers 2 --horizon 3
  python tools/optimize.py --budget 100.3                                                   # from scratch

Inputs: data_pull/ (tools/fetch_latest.py) and a projection table (tools/fplform_snippets.md).
Players are matched on (web_name, team) to FPL ids; anything unmatched is listed, never guessed.

Money: --bank is "Money Remaining" in the app. Players you own count at their SELLING price
(you keep only half of any rise), which is worked out from your public transfer history when
--me is given; players bought since the last deadline, or everyone when --me is absent, count at
today's price.

The evidence gate (on by default; --no-gate turns it off) scores a player as zero when he starts,
so the optimiser only starts him if nobody else can fill the slot. For the whole window when the
season so far contradicts the projection:
  * 180+ minutes, and both his points per 90 and the points per 90 his underlying numbers imply
    (xG, xA, xGC, defensive contributions, saves) under 60% of his projected points per match (a
    double gameweek counts as two matches). Points alone never gate: five games of points are
    mostly noise, so a player the underlying numbers back keeps his projection; or
  * 0 minutes so far and a price of 5.0+ (nothing yet to say he plays), or
  * status "unavailable" (left the club, long-term absence).
For the next gameweek only, when FPL flags him 0% to play, injured, suspended or ineligible:
those flags describe the next round, and the projections already price in when he returns.
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, Projections, read_squad_file, solve_squad

ORDER = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}


def gate(proj, data):
    """{id: (reason, weeks)}: weeks is None for the whole window, else the set of gw labels."""
    out = {}
    nxt = f"gw{data.nxt_gw}" if data.nxt_gw else None
    first = {nxt} if nxt in proj.gws else set()        # flags speak about the next round only
    for el, r in proj.rows.items():
        p = data.players[el]
        mins = float(p["minutes"] or 0); pts = float(p["total_points"] or 0)
        matches = sum(proj.fixtures(el, g) for g in proj.gws)
        per_match = r["next"] / matches if matches else 0.0
        chance = p.get("chance_of_playing_next_round")
        status = p.get("status")
        if status == "u":
            out[el] = ("unavailable", None)
        elif (mins >= 180 and per_match > 0 and pts / (mins / 90) < 0.6 * per_match
              and (data.underlying_per90(el) or 0) < 0.6 * per_match):
            out[el] = (f"{pts / (mins / 90):.1f} pts/90, {data.underlying_per90(el):.1f} underlying, vs "
                       f"{per_match:.1f} projected per match", None)
        elif mins == 0 and r["cost"] >= 5.0 and r["next"] > 0:
            out[el] = ("no minutes yet", None)
        elif first and ((chance not in (None, "", "None") and float(chance) == 0) or status in ("i", "s", "n")):
            why = {"i": "injured", "s": "suspended", "n": "ineligible"}.get(status, "0% to play")
            out[el] = (f"{why} for {nxt}", first)
    return out


def resolve(names, data):
    by_key = {f"{p['web_name']} ({p['team']})": el for el, p in data.players.items()}
    out = []
    for s in (x.strip() for x in names.split(";")):
        if not s:
            continue
        if s not in by_key:
            sys.exit(f"unknown player {s!r} — write it as 'web_name (TEAM)'")
        out.append(by_key[s])
    return out


def show(proj, res, title, base=None, gated=None):
    gws = proj.gws; R = proj.rows
    print("=" * 100)
    print(f"{title} | projected {res['total']:.1f} over {len(gws)} GW ({gws[0]}-{gws[-1]}) | £{res['cost']:.1f}m")
    print(f"{'pos':4}{'player':26}{'£':>5}{'proj':>6}{'st':>3}{'C':>2}  " + " ".join(f"{g:>5}" for g in gws))
    for i in sorted(res["squad"], key=lambda i: (ORDER[R[i]["pos"]], -R[i]["next"])):
        r = R[i]
        st = sum(1 for g in gws if i in res["lineups"][g]); cp = sum(1 for g in gws if res["caps"][g] == i)
        flag = ("" if base is None or i in base else "  IN") + (f"  [gated, scored 0: {gated[i][0]}]" if gated and i in gated else "") \
            + ("" if r["projected"] else "  [no projection]")
        print(f"{r['pos']:4}{proj.label(i)[:25]:26}{r['cost']:5.1f}{r['next']:6.1f}{st:3d}{cp:2d}  "
              + " ".join(f"{r['gw'][g]:5.1f}" for g in gws) + flag)
    if base and not title.startswith("Hold"):
        out = [i for i in base if i not in res["squad"]]
        if out:
            print("OUT:", ", ".join(proj.label(i) for i in out))
            print("IN :", ", ".join(proj.label(i) for i in res["squad"] if i not in base))
        else:
            print("no transfer beats holding")
    print("captains:", ", ".join(f"{g}:{R[res['caps'][g]]['name']}" for g in gws))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--proj", default="proj.txt")
    ap.add_argument("--squad", help="current squad file (15 lines)")
    ap.add_argument("--transfers", type=int, help="max changes from --squad (default: free choice)")
    ap.add_argument("--budget", type=float, help="total money, for a squad from scratch only")
    ap.add_argument("--bank", type=float, help="money in the bank; required with --squad")
    ap.add_argument("--me", default=__import__("os").environ.get("FPL_ME"), help="your alias, for selling prices")
    ap.add_argument("--must", default="", help="'Name (TEAM);Name (TEAM)' to force in")
    ap.add_argument("--ban", default="", help="'Name (TEAM);...' to exclude")
    ap.add_argument("--horizon", type=int, help="use only the first N gameweeks of the window")
    ap.add_argument("--ros-weight", type=float, default=0.0, help="weight on rest-of-season points")
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--max-reserves", type=int, help="at most N squad players under a 50%% chance to appear "
                    "(non-playing bench fodder); the optimiser does not value bench cover, so this keeps it")
    a = ap.parse_args()

    data = Data(a.data)
    proj = Projections(a.proj, data, a.horizon)
    print(f"{len(proj.rows)} projected players matched to FPL ids, window {proj.gws[0]}..{proj.gws[-1]}"
          + (f"; UNMATCHED {len(proj.unmatched)}: {', '.join(proj.unmatched[:12])}" if proj.unmatched else "; all rows matched"))
    if proj.dropped:
        print(f"(ignoring {', '.join(proj.dropped)}: already locked in)")
    if proj.gap:
        print(f"! the projections start at {proj.gws[0]} but the next deadline is GW{data.nxt_gw}: fetch fresh ones")
    base = read_squad_file(a.squad, data) if a.squad else None
    sell = {}
    if base:
        if a.bank is None:
            sys.exit("pass --bank (Money Remaining in the app) with --squad")
        added = proj.ensure(base)
        if added:
            print("no projection (scored as 0):", ", ".join(proj.label(i) for i in added))
        if a.me:
            sell = data.selling_prices(data.resolve_entry(a.me), base)
            lower = {i: v for i, v in sell.items() if v < proj.rows[i]["cost"] - 1e-9}
            if lower:
                print("selling below today's price:", ", ".join(f"{proj.label(i)} {v:.1f}" for i, v in lower.items()))
        else:
            print("no --me: owned players valued at today's price (selling prices can be lower)")
    gated = {} if a.no_gate else gate(proj, data)
    nostart = {i: weeks for i, (_, weeks) in gated.items()}
    if gated:
        print(f"{len(gated)} players scored as zero if started (evidence gate); the ones that matter:")
        for i in sorted(gated, key=lambda i: -proj.rows[i]["next"])[:10]:
            print(f"   {proj.label(i):28} proj {proj.rows[i]['next']:5.1f}  {gated[i][0]}")
    if base:
        budget = a.bank + sum(sell.get(i, proj.rows[i]["cost"]) for i in base)
    else:
        budget = a.budget if a.budget is not None else 100.0
    limits = []
    if a.max_reserves is not None:
        reserves = {el for el, _, _ in proj.scaled} | {i for i, r in proj.rows.items() if not r["projected"]}
        limits.append((reserves, a.max_reserves))
    res = solve_squad(proj, budget, base=base, transfers=a.transfers, must=resolve(a.must, data),
                      ban=resolve(a.ban, data), no_start=nostart, ros_weight=a.ros_weight, sell=sell, limits=limits)
    if base and a.transfers is not None:
        hold = solve_squad(proj, budget, base=base, transfers=0, no_start=nostart, sell=sell)
        show(proj, hold, "Hold (no transfers)", base, gated)
        title = f"Best with up to {a.transfers} transfer(s)"
        show(proj, res, title, base, gated)
        print(f"\ngain over holding: {res['total'] - hold['total']:+.1f} projected points "
              f"(a hit costs 4; budget £{budget:.1f}m)")
    else:
        show(proj, res, "Best squad" + (f" (budget £{budget:.1f}m)"), base, gated)


if __name__ == "__main__":
    main()
