#!/usr/bin/env python3
"""
Simulate the mini-league to the end of the season: chance of finishing 1st-4th, of the top three,
of any prize, and the prize money it is worth, for your squad and any alternatives.

  python tools/fetch_latest.py owner/repo
  python tools/run_simulation.py --me <alias> --proj proj.txt
  python tools/run_simulation.py --me <alias> --my-squad my_squad.local.txt --my-bank 0.6 \\
        --committed WC --candidate alt.local.txt --k-by auto --names names.local.json

Everything comes from the pulled data: running totals (plus any auto-sub points FPL has not yet
added), chips still available (both halves of the season handled), banks, and each manager's squad
with Free Hits reverted. FPL shows a Wildcard or transfers made for the next deadline only once
that deadline passes, so pass your real squad with --my-squad and the chip with --committed until
then. Keep that file out of the repo: it would show rivals your team before the deadline.
A committed Bench Boost, Triple Captain or Free Hit is scored in the next gameweek (bench points,
the captain's points again, or the best one-week squad minus the held one); with a Free Hit,
--my-squad is the squad you revert to, not the Free Hit squad.

Rival skill (k, good transfers each rival makes over the projection window) moves the answer more
than anything else, so the report always shows the uniform-k answer, and adds the tiered one when
--k-by is given ('auto' derives tiers from rate_transfers.py).

Two different questions, answered separately:
  FORECAST    where you are likely to finish. You get the same k as the rivals (like for like).
  CANDIDATES  which squad to pick now. Each candidate is scored as it stands, with no future
              transfers for you: granting them would let a weak squad "fix itself" in the model and
              hide exactly the difference being measured.
"""
import argparse, copy, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, Projections, read_squad_file, solve_squad, variance_bands, club_correlation
from simulate import simulate, CHIP_EV
import rate_transfers


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--proj", default="proj.txt")
    ap.add_argument("--me", default=os.environ.get("FPL_ME"), help="your alias as in league.csv, or your entry id with FPL_ID_SALT set (default $FPL_ME)")
    ap.add_argument("--my-squad", help="your real squad when it differs from what FPL shows publicly")
    ap.add_argument("--my-bank", type=float, help="your bank after those moves (default: FPL's last public figure)")
    ap.add_argument("--candidate", action="append", default=[], help="alternative squad file for you; repeatable")
    ap.add_argument("--committed", action="append", default=[], help="CHIP you have played for the next deadline (WC, FH, BB, TC), or ALIAS:CHIP for someone else")
    ap.add_argument("--k", type=int, default=3, help="good transfers per rival over the window (uniform case)")
    ap.add_argument("--k-by", default="", help="'auto' or 'entry:k,entry:k' for the tiered case")
    ap.add_argument("--my-k", type=int, help="your own future good transfers in the forecast (default: same as --k)")
    ap.add_argument("--prizes", default="275,125,100,50", help="prize for 1st,2nd,... in money")
    ap.add_argument("--sims", type=int, default=100_000); ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--sigma", type=float, help="weekly spread across the league; measured if omitted")
    ap.add_argument("--horizon", type=int, help="use only the first N gameweeks of the projections")
    ap.add_argument("--names", help="JSON {alias: display name}; keep it out of the repo")
    a = ap.parse_args()
    if not a.me:
        sys.exit("pass --me <your alias> or set FPL_ME")
    data = Data(a.data)
    me = data.resolve_entry(a.me)
    proj = Projections(a.proj, data, a.horizon)
    names = json.loads(Path(a.names).read_text(encoding="utf-8")) if a.names else {}
    nm = lambda e: ("> " if e == me else "  ") + names.get(e, e)
    prizes = [float(x) for x in a.prizes.split(",")]
    committed = {}
    for c in a.committed:
        e, chip = c.split(":") if ":" in c else (me, c)
        e, chip = data.resolve_entry(e), chip.upper()
        if chip not in CHIP_EV:
            sys.exit(f"--committed {c}: the chip must be one of {', '.join(CHIP_EV)}")
        if chip not in data.chips_left(e):
            sys.exit(f"--committed {c}: {names.get(e, e)} has no {chip} left in this half of the season")
        committed.setdefault(e, []).append(chip)
    if any(len(v) > 1 for v in committed.values()):
        sys.exit("--committed: only one chip can be played per gameweek")

    order = [str(r["entry"]) for r in data.league]
    row = {str(r["entry"]): r for r in data.league}
    squads = {e: data.squad(e) for e in order}
    source = {e: ("Free Hit reverted" if row[e].get("free_hit_reverted") == "True" else f"GW{data.cur_gw} picks") for e in order}
    bank = {e: float(row[e]["bank"] or 0) for e in order}
    if a.my_squad:
        squads[me] = read_squad_file(a.my_squad, data); source[me] = f"{Path(a.my_squad).name}"
        if a.my_bank is None:
            print("! --my-squad without --my-bank: using the bank FPL showed at the last deadline")
        else:
            bank[me] = a.my_bank
    for e in order:
        proj.ensure(squads[e])
    sell = {e: data.selling_prices(e, squads[e]) for e in order}
    budget = {e: bank[e] + sum(sell[e].values()) for e in order}
    subs = {e: data.pending_autosubs(e) for e in order}
    start = {e: float(row[e]["total"] or 0) + subs[e] for e in order}
    left = {e: data.chips_left(e, committed.get(e, ())) for e in order}
    nxt = data.nxt_gw or data.cur_gw
    weeks_in_half = (19 if nxt <= 19 else 38) - nxt + 1      # one chip per gameweek; unused ones expire
    # a chip committed for the next deadline occupies that week
    slots = {e: weeks_in_half - (1 if committed.get(e) else 0) for e in order}
    chips = {e: sum(sorted((CHIP_EV[c] for c in left[e]), reverse=True)[:max(0, slots[e])]) for e in order}
    g0 = f"gw{nxt}"
    if any(c in ("BB", "TC", "FH") for cs in committed.values() for c in cs) and g0 not in proj.gws:
        print(f"! a committed chip is for GW{nxt}, which the projections do not cover: it is not scored")
    sigma, nweeks = (a.sigma, 0) if a.sigma else data.sigma()
    vb, cc = variance_bands(data), club_correlation(data)

    if not data.fixtures_finished():
        print(f"! GW{data.cur_gw} is still being played: totals are part-way and the rest of it is not simulated."
              " Rerun once its last match is over.")
    if proj.gap:
        print(f"! the projections start at {proj.gws[0]} but the next deadline is GW{nxt}: fetch fresh ones")
    print(f"GW{data.cur_gw} {'finished' if data.fixtures_finished() else 'in progress'}; projections {proj.gws[0]}-{proj.gws[-1]}"
          + (f" ({', '.join(proj.dropped)} ignored: already locked in)" if proj.dropped else "")
          + f", {len(proj.rows)} players ({len(proj.unmatched)} unmatched)")
    print(f"weekly spread across the league {sigma:.1f}" + (f" (measured over {nweeks} gameweeks, chip weeks excluded)" if nweeks else " (given)"))
    print(f"teammate correlation: keeper/defenders {cc['dd']:.2f}, defender-attacker {cc['da']:.2f}, attackers {cc['aa']:.2f}")
    print(f"\n{'manager':24}{'total':>6}{'+subs':>6}{'chips left':>13}{'bank':>6}  squad")
    for e in order:
        now = f"  (playing {'+'.join(committed[e])} in GW{nxt})" if committed.get(e) else ""
        print(f"{nm(e)[:23]:24}{row[e]['total']:>6}{subs[e]:>+6d}{' '.join(left[e]) or '-':>13}{bank[e]:6.1f}  {source[e]}{now}")

    cands = {"your squad": squads[me]}
    for f in a.candidate:
        cands[Path(f).stem] = read_squad_file(f, data)
        proj.ensure(cands[Path(f).stem])
    my_k = a.k if a.my_k is None else a.my_k
    cache = {}

    def sol(e, sq, k):
        key = (e, tuple(sorted(sq)), k)
        if key not in cache:
            sp = data.selling_prices(e, sq)
            cache[key] = solve_squad(proj, bank[e] + sum(sp.values()), base=sq, transfers=k, sell=sp)
        return cache[key]

    def chip_now(e, s):
        """Points a chip committed for the next gameweek adds that week, given the manager's plan."""
        if g0 not in proj.gws:
            return 0.0
        R, v = proj.rows, 0.0
        for c in committed.get(e, ()):
            if c == "BB":
                v += sum(R[i]["gw"][g0] for i in s["squad"] if i not in s["lineups"][g0])
            elif c == "TC":
                v += R[s["caps"][g0]]["gw"][g0]
            elif c == "FH":
                one = copy.copy(proj); one.gws = [g0]
                fh = solve_squad(one, budget[e])
                v += fh["total"] - sum(R[i]["gw"][g0] * w for i, w in s["weights"][g0].items())
        return v

    def run(kmap, sq, mk):
        sols = {e: sol(e, squads[e], kmap[e]) for e in order if e != me}
        sols[me] = sol(me, sq, mk)
        ch = {e: chips[e] + chip_now(e, sols[e]) for e in order}
        return simulate(order, proj, squads, budget, start, ch, kmap, me, vb, cc, sigma,
                        prizes=prizes, nsim=a.sims, seed=a.seed, sols=sols)

    head = f"{'':24}{'window':>7}{'P(1st)':>8}{'P(2nd)':>8}{'P(3rd)':>8}{'P(4th)':>8}{'top 3':>8}{'money':>8}{'EV':>8}"

    def line(label, out):
        j = order.index(me); P = out["P"][j]
        return (f"{label[:23]:24}{out['window'][me]:7.1f}" + "".join(f"{100 * P[r]:7.1f}%" for r in range(4))
                + f"{100 * P[:3].sum():7.1f}%{100 * P[:len(prizes)].sum():7.1f}%{out['ev'][j]:8.1f}")

    def table(out):
        print(f"{'':24}{'window':>7}{'P(1st)':>8}{'top 3':>8}{'E[rank]':>8}{'you beat':>9}{'corr':>6}")
        for j in sorted(range(len(order)), key=lambda j: -out["P"][j][0]):
            e = order[j]
            h2h = "" if e == me else f"{100 * out['head_to_head'][e]:8.0f}%"
            corr = "" if e == me else f"{out['corr_with_me'][e]:6.2f}"
            print(f"{nm(e)[:23]:24}{out['window'][e]:7.1f}{100 * out['P'][j][0]:7.1f}%{100 * out['P'][j][:3].sum():7.1f}%"
                  f"{out['mean_rank'][j]:8.2f}{h2h:>9}{corr:>6}")
        print(f"(model spread before calibration {out['model_sd']:.1f}/week; variance scaled x{out['scale']:.2f} to match the league)")

    uniform = {e: a.k for e in order}
    kmaps = [(f"skill ignored: every rival makes {a.k} good transfers over the window", uniform)]
    if a.k_by:
        if a.k_by == "auto":
            ratings, _ = rate_transfers.rate_all(data)
            tiers = rate_transfers.tiers(ratings, exclude={me})
        else:
            tiers = {x.split(":")[0]: int(x.split(":")[1]) for x in a.k_by.split(",") if x}
        kmap = {e: tiers.get(e, a.k) for e in order}
        kmaps.append(("skill tiered by transfer record: " + ", ".join(f"{names.get(e, e)} {kmap[e]}" for e in order if e != me), kmap))

    print(f"\n== FORECAST: your squad, and you also make {my_k} good transfers ==")
    print(head)
    outs = []
    for label, km in kmaps:
        out = run(km, squads[me], my_k)
        outs.append((label, out))
        print(line("skill ignored" if km is uniform else "skill tiered", out))
    for label, out in outs:
        print(f"\n{label}")
        table(out)

    if len(cands) > 1:
        print("\n== CANDIDATES, each scored as it stands (your future transfers left out, so the squads' own"
              " differences show; rivals as in the skill-ignored case) ==")
        print(head + f"{'vs yours':>10}")
        base = None
        for cname, sq in cands.items():
            out = run(uniform, sq, 0)
            j = order.index(me)
            base = base if base is not None else out["ev"][j]
            print(line(cname, out) + f"{out['ev'][j] - base:+10.1f}")


if __name__ == "__main__":
    main()
