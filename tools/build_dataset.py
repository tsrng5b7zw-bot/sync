#!/usr/bin/env python3
"""
Turn the fetched data (and a projection dump, if present) into plain-text working files:

  actuals.txt         every player who has played: price, points, minutes, goals, assists, xG, xA,
                      defensive contributions, clean sheets, bonus, cards, penalty order, status, id
  proj.txt            the projection dump, checked: gw<n> columns present, every row matched to an FPL id
  squad_next.txt      your squad for the next gameweek as FPL shows it publicly (Free Hit reverted)
  league_summary.txt  standings with auto-sub points FPL still owes, chips left, bank, captain, squads,
                      and which players several managers share

  python tools/build_dataset.py [--data data_pull] [--proj proj_raw.txt] [--out .] [--me ALIAS]

FPL keeps transfers and chips for the next deadline private until it passes, so squad_next.txt is
the squad as of the last deadline. If you have made moves since, write your real squad into a file
of your own (e.g. my_squad.local.txt, which .gitignore excludes) and pass it to the tools with
--squad / --my-squad; never commit it.
"""
import argparse, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fplcommon import Data, Projections, read_csv, write_squad_file

STATUS = {"a": "Available", "d": "Doubtful", "i": "Injured", "s": "Suspended", "u": "Unavailable", "n": "Not in squad"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_pull"); ap.add_argument("--proj", default="proj_raw.txt")
    ap.add_argument("--out", default="."); ap.add_argument("--me", default=os.environ.get("FPL_ME"))
    a = ap.parse_args()
    data = Data(a.data); O = Path(a.out); O.mkdir(parents=True, exist_ok=True)
    me = data.resolve_entry(a.me) if a.me else None

    cols = ["name", "pos", "team", "price", "pts", "min", "G", "A", "defcon", "CS", "bonus", "YC", "pen", "chance",
            "status", "sel", "bps", "id", "xG", "xA", "xGC", "starts"]
    lines = ["|".join(cols)]
    for p in sorted(data.players.values(), key=lambda r: -int(r["total_points"] or 0)):
        if int(p["minutes"] or 0) == 0 and int(p["total_points"] or 0) == 0:
            continue
        chance = p["chance_of_playing_next_round"]
        chance = "100" if chance in (None, "", "None") else chance
        pen = "" if p["penalties_order"] in (None, "", "None") else p["penalties_order"]
        lines.append("|".join(str(x) for x in [
            p["web_name"], p["pos"], p["team"], p["price"], p["total_points"], p["minutes"], p["goals_scored"], p["assists"],
            p["defensive_contribution"], p["clean_sheets"], p["bonus"], p["yellow_cards"], pen, chance,
            STATUS.get(p["status"], p["status"]), p["selected_by_percent"], p["bps"], p["id"], p["expected_goals"],
            p["expected_assists"], p["expected_goals_conceded"], p["starts"]]))
    (O / "actuals.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"actuals.txt: {len(lines) - 1} players with minutes or points")

    pr = Path(a.proj)
    if pr.exists():
        proj = Projections(pr, data)
        (O / "proj.txt").write_text(pr.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"proj.txt: {len(proj.rows)} players matched to FPL ids, window {proj.gws[0]}..{proj.gws[-1]}"
              + (f" ({', '.join(proj.dropped)} already locked in, ignored by the tools)" if proj.dropped else ""))
        if proj.gap:
            print(f"  ! the projections skip GW{data.nxt_gw}: fetch fresh ones before deciding anything")
        if proj.unmatched:
            print(f"  ! {len(proj.unmatched)} rows match no FPL player (renamed or transferred?): {', '.join(proj.unmatched[:15])}")
        if proj.scaled:
            big = sorted(proj.scaled, key=lambda t: -t[2] * (1 - t[1]))[:6]
            print(f"  {len(proj.scaled)} players under a 50% chance to appear are weighted by that chance, e.g. "
                  + ", ".join(f"{data.label(el)} {tot:.1f}->{tot * pr:.1f}" for el, pr, tot in big))
    else:
        print(f"no {pr} — read the projections first (tools/fplform_snippets.md)")

    order = [str(r["entry"]) for r in data.league]
    out = [f"pulled {data.meta.get('pulled_at_utc')} | GW{data.cur_gw} "
           f"{'finished' if data.fixtures_finished() else 'in progress'} | next GW{data.nxt_gw} deadline {data.meta.get('next_deadline_utc')} UTC",
           "", "rank | entry | total (+auto-subs owed) | GW pts | chips used | chips left | bank | value | captain"]
    for r in data.league:
        e = str(r["entry"]); owed = data.pending_autosubs(e)
        out.append(f"{r['rank']} | {e}{' (you)' if e == me else ''} | {r['total']}{f' (+{owed})' if owed else ''} | "
                   f"{r['event_total']} | {r['chips_used'] or '-'} | {' '.join(data.chips_left(e)) or '-'} | "
                   f"{r['bank']} | {r['value']} | {r['captain']}")
    out += ["", "squads carrying into the next gameweek (Free Hits reverted):"]
    for r in data.league:
        out.append(f"  {r['entry']}{' [Free Hit reverted]' if r.get('free_hit_reverted') == 'True' else ''}: {r['squad']}")
    if (data.D / "ownership.csv").exists():
        out += ["", "players owned by two or more managers:"]
        for r in read_csv(data.D / "ownership.csv"):
            if int(r["owners"]) >= 2:
                out.append(f"  {r['web_name']} ({r['team']}) x{r['owners']}: {r['managers']}")
    (O / "league_summary.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"league_summary.txt: {len(order)} managers")

    if me:
        write_squad_file(O / "squad_next.txt", data.squad(me), data,
                         header=f"{me} as of the GW{data.cur_gw} deadline; moves made since are not visible until GW{data.nxt_gw}'s deadline")
        print("squad_next.txt: your public squad (edit a copy if you have made moves since the last deadline)")
    else:
        print("no --me / FPL_ME, so squad_next.txt was not written")


if __name__ == "__main__":
    main()
