#!/usr/bin/env python3
"""
How good has each manager's transferring been? Points actually gained by each gameweek's
transfers, measured on the lineups the manager really played.

  python tools/rate_transfers.py [--data data_pull] [--names names.local.json]

For every transfer window (all the moves a manager made for one gameweek, netted so a player
bought and sold in the same week cancels), players are grouped by position. For each later
gameweek while the incoming players are still owned:

    gain = m * (points of the incoming players still owned - their share of the outgoing players' points)

where m is the average slot those incoming players actually filled that week: 1 in the XI, 0 on
the bench (Bench Boost puts everyone in). Captaincy is excluded on purpose: armband choice is a
separate skill, and doubling one hauler would swamp everything else. Using the same average slot
for both sides means the answer does not depend on which outgoing player is "paired" with which
incoming one, which is arbitrary on a Wildcard.

A Free Hit is scored for its own week only, and the other windows skip Free Hit weeks rather
than stopping at them (the squad comes back the week after). A Wildcard counts until each player
is sold. Hits are subtracted in the window they were taken. Points are summed per gameweek, so
double gameweeks count both matches.

"robust" drops each manager's single best window: one lucky haul should not make a genius. With
only a few windows per manager this is still a noisy measure; treat tiers as a sensitivity, not a
verdict.
"""
import argparse, json, sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, read_csv

POS_ORDER = ("GK", "DEF", "MID", "FWD")


def points_by_round(data):
    """{(element, round): points}. Daily history first; the current gameweek's live file fills in
    anything the daily job has not caught up with yet."""
    pts = {k: v[0] for k, v in data.round_points().items()}
    live = data.D / f"live_gw{data.cur_gw}.csv"
    if live.exists():                                  # fresher, and already summed over a double
        for r in read_csv(live):
            pts[(int(r["id"]), data.cur_gw)] = int(float(r["total_points"] or 0))
    return pts


def last_scored_gw(data):
    return data.last_finished_gw()


def rate_entry(data, eid, pts, last_gw):
    picks = {}
    for g in range(1, last_gw + 1):
        pk = data.picks(eid, g)
        if pk:
            picks[g] = {p["element"]: p["multiplier"] for p in pk["picks"]}
    hist = data.history(eid)
    hit = {x["event"]: x.get("event_transfers_cost", 0) for x in hist.get("current", [])}
    chip = {c["event"]: c["name"] for c in hist.get("chips", [])}
    free_hits = {g for g, n in chip.items() if n == "freehit"}
    by_gw = defaultdict(list)
    for t in data.transfers(eid):
        if t["event"] <= last_gw:
            by_gw[t["event"]].append(t)
    windows = []
    for g in sorted(by_gw):
        ins = Counter(t["element_in"] for t in by_gw[g])
        outs = Counter(t["element_out"] for t in by_gw[g])
        net_in = list((ins - outs).elements())          # A->B, B->A, A->C nets to A->C
        net_out = list((outs - ins).elements())
        gain = 0.0
        for pos in POS_ORDER:
            pin = [e for e in net_in if data.players.get(e, {}).get("pos") == pos]
            pout = [e for e in net_out if data.players.get(e, {}).get("pos") == pos]
            if not pin:
                continue
            owned = list(pin)
            weeks = [g] if g in free_hits else range(g, last_gw + 1)
            for h in weeks:
                if h in free_hits and h != g:
                    continue                                          # someone else's squad that week
                if h not in picks:
                    break
                owned = [e for e in owned if e in picks[h]]          # tenure ends at first absence
                if not owned:
                    break
                m = sum(min(picks[h][e], 1) for e in owned) / len(owned)
                share = len(owned) / len(pin)
                gain += m * (sum(pts.get((e, h), 0) for e in owned) - share * sum(pts.get((e, h), 0) for e in pout))
        gain -= hit.get(g, 0)
        windows.append({"gw": g, "moves": len(net_in), "hit": hit.get(g, 0), "chip": chip.get(g, ""),
                        "gain": gain, "in": net_in, "out": net_out})
    total = sum(w["gain"] for w in windows)
    robust = total - max(w["gain"] for w in windows) if len(windows) > 1 else 0.0
    return {"entry": str(eid), "windows": windows, "total": total, "robust": robust,
            "moves": sum(w["moves"] for w in windows), "hits": sum(w["hit"] for w in windows)}


def rate_all(data):
    pts = points_by_round(data)
    last = last_scored_gw(data)
    return [rate_entry(data, r["entry"], pts, last) for r in data.league], last


def tiers(ratings, exclude=()):
    """Skill tiers for the simulator: quartiles of the robust score -> 4, 3, 2, 1 good transfers
    over the projection window."""
    rs = sorted((r for r in ratings if r["entry"] not in exclude), key=lambda r: -r["robust"])
    n = len(rs)
    return {r["entry"]: 4 - min(3, (4 * i) // max(1, n)) for i, r in enumerate(rs)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull")
    ap.add_argument("--names", help="optional JSON {alias: display name}; keep it out of the repo")
    ap.add_argument("--me", default=__import__("os").environ.get("FPL_ME"), help="your alias (excluded from the rivals' tiers)")
    ap.add_argument("--detail", action="store_true", help="print every window")
    a = ap.parse_args()
    data = Data(a.data)
    names = json.loads(Path(a.names).read_text(encoding="utf-8")) if a.names else {}
    nm = lambda e: names.get(str(e), str(e))
    ratings, last = rate_all(data)
    print(f"transfer gains through GW{last} (captaincy excluded, hits subtracted)\n")
    print(f"{'manager':22}{'windows':>8}{'moves':>7}{'hits':>6}{'gain':>8}{'robust':>8}   best window")
    for r in sorted(ratings, key=lambda r: -r["robust"]):
        best = max(r["windows"], key=lambda w: w["gain"], default=None)
        bw = f"GW{best['gw']}{' ' + best['chip'] if best['chip'] else ''} {best['gain']:+.0f}" if best else "-"
        print(f"{nm(r['entry'])[:21]:22}{len(r['windows']):8d}{r['moves']:7d}{r['hits']:6d}{r['total']:+8.1f}{r['robust']:+8.1f}   {bw}")
        if a.detail:
            for w in r["windows"]:
                print(f"     GW{w['gw']:<3}{w['chip']:8}{w['moves']:2d} moves  hit {w['hit']:<2} gain {w['gain']:+6.1f}   "
                      f"out {', '.join(data.label(e) for e in w['out'])}  |  in {', '.join(data.label(e) for e in w['in'])}")
    me = data.resolve_entry(a.me) if a.me else None
    t = tiers(ratings, exclude={me} if me else ())
    print("\nsuggested skill tiers for run_simulation.py (k = good transfers over the window):")
    print("  --k-by " + ",".join(f"{e}:{k}" for e, k in t.items()))


if __name__ == "__main__":
    main()
