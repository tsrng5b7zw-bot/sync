#!/usr/bin/env python3
"""
FPL collector. Pulls the public Fantasy Premier League API into data/latest/ (JSON + CSV).

Usage:
  python collector/pull.py            # hourly job: bootstrap, fixtures, league, entries, live points, prices
  python collector/pull.py --elements # daily job: per-player element-summary (per-GW minutes, xG, xA, bps ...)

Everything here is public data (no login). fplform.com is NOT pulled here: its data is copyrighted and
must be read live in a browser (see tools/fplform_snippets.md).

Nothing written here identifies the league or anyone in it. The league's standings response (league
name, team names, managers' real names) is never saved, and every FPL entry id is replaced by an alias
derived with a secret key (FPL_ID_SALT), so a rival searching for their own team id finds nothing.
Only someone holding the key can map an alias back to a team.
"""
import argparse, csv, datetime as dt, hashlib, hmac, json, os, re, shutil, sys, time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("pip install requests")

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "collector" / "config.json").read_text(encoding="utf-8"))
# Both come from repository secrets, so neither the league id nor the key is ever committed.
LEAGUE_ID = int(os.environ.get("FPL_LEAGUE_ID") or CFG.get("league_id") or 0)
SALT = os.environ.get("FPL_ID_SALT", "")


def alias(entry_id):
    """Stable, unguessable stand-in for an FPL entry id: 'm' + 8 hex characters of an HMAC."""
    return "m" + hmac.new(SALT.encode(), str(entry_id).encode(), hashlib.sha256).hexdigest()[:8]


RANK_KEYS = ("rank", "rank_sort", "overall_rank", "percentile_rank", "overall_rank_percentage")


def scrub_history(h):
    """Drop what could fingerprint a manager (overall ranks, previous seasons); keep the rest."""
    h = dict(h)
    h.pop("past", None)
    h["current"] = [{k: v for k, v in x.items() if k not in RANK_KEYS} for x in h.get("current", [])]
    return h


def scrub_picks(pk, a):
    pk = dict(pk)
    if isinstance(pk.get("entry_history"), dict):
        pk["entry_history"] = {k: v for k, v in pk["entry_history"].items() if k not in RANK_KEYS}
    pk["automatic_subs"] = [{**s, "entry": a} for s in pk.get("automatic_subs", [])]
    return pk


def scrub_transfers(tr, a):
    return [{**t, "entry": a} for t in tr]


OUT = ROOT / "data" / "latest"
API = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal fantasy analytics)"}
PAUSE = float(CFG.get("request_pause_seconds", 0.25))

POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
CHIP_NAMES = {"wildcard": "WC", "freehit": "FH", "bboost": "BB", "3xc": "TC"}

PLAYER_COLS = [
    "id", "web_name", "first_name", "second_name", "team", "pos", "now_cost", "total_points", "event_points",
    "minutes", "starts", "goals_scored", "assists", "expected_goals", "expected_assists",
    "expected_goal_involvements", "expected_goals_conceded", "clean_sheets", "goals_conceded",
    "defensive_contribution", "clearances_blocks_interceptions", "recoveries", "tackles", "bonus", "bps",
    "yellow_cards", "red_cards", "saves", "penalties_saved", "penalties_missed", "own_goals",
    "penalties_order", "corners_and_indirect_freekicks_order", "direct_freekicks_order",
    "chance_of_playing_next_round", "chance_of_playing_this_round", "status", "news", "news_added",
    "selected_by_percent", "transfers_in_event", "transfers_out_event", "transfers_in", "transfers_out",
    "cost_change_event", "cost_change_start", "form", "points_per_game", "ep_next", "ep_this", "value_season",
]


def redact(text):
    """Logs of a public repo's Actions runs are public too: never print a league or entry id."""
    return re.sub(r"(leagues-classic|entry)/\d+", r"\1/<id>", str(text))


def get(path, retries=3):
    url = f"{API}/{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                time.sleep(PAUSE)
                return r.json()
            if r.status_code == 404:
                return None
            print(f"  ! {redact(path)}: HTTP {r.status_code}", file=sys.stderr)
        except requests.RequestException as e:  # noqa: BLE001
            print(f"  ! {redact(path)}: {redact(e)}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    print(f"  ! giving up on {redact(path)}", file=sys.stderr)
    return None


def dump(obj, rel):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return p


def write_csv(rows, rel, cols):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def chips_left(chips, gw):
    """Chips still available for gameweek `gw`. Each chip comes twice: once for GW1-19,
    once for GW20-38; a chip used in one half does not consume the other half's copy."""
    first_half = (gw or 1) <= 19
    used = {CHIP_NAMES.get(c["name"], c["name"]) for c in chips if (c["event"] <= 19) == first_half}
    return sorted({"WC", "FH", "BB", "TC"} - used)


def effective_squad(picks_by_gw, cur_gw):
    """The squad that carries into the next gameweek. A Free Hit squad lasts one week and then
    reverts, so if the current week's picks were made on a Free Hit, the real squad is the
    previous week's. Returns (element ids, reverted?)."""
    cur = picks_by_gw.get(cur_gw) or {}
    if cur.get("active_chip") == "freehit" and picks_by_gw.get((cur_gw or 0) - 1):
        return [p["element"] for p in picks_by_gw[cur_gw - 1]["picks"]], True
    return [p["element"] for p in cur.get("picks", [])], False


def season_of(boot):
    """'2026' for 2026/27: the year of the first gameweek's deadline. Player ids and gameweek
    numbers restart every season, so anything cached from last season must go."""
    ev = (boot or {}).get("events") or [{}]
    return str(ev[0].get("deadline_time") or "")[:4]


def new_season_cleanup(season):
    """Remove last season's cached picks, live files and per-player histories when the season
    changes. Returns True if it cleaned anything."""
    mp = OUT / "meta.json"
    old = json.loads(mp.read_text(encoding="utf-8")).get("season") if mp.exists() else None
    if not season or old == season or not mp.exists():
        return False
    if old is None:                         # meta written before seasons were recorded: same season
        return False
    for d in ("entries", "elements"):
        if (OUT / d).exists():
            shutil.rmtree(OUT / d)
    for f in list(OUT.glob("live_gw*")) + [OUT / "player_gw_history.csv"]:
        if f.exists():
            f.unlink()
    print(f"new season {season} (was {old}): cleared last season's cached files")
    return True


def current_and_next(events):
    cur = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)
    return cur, nxt


def main_hourly():
    if not LEAGUE_ID or len(SALT) < 16:
        sys.exit("Set repository secrets FPL_LEAGUE_ID and FPL_ID_SALT (a random string of 16+ characters)")
    started = dt.datetime.now(dt.timezone.utc)
    print("bootstrap-static ...")
    boot = get("bootstrap-static/")
    if not boot:
        sys.exit("bootstrap failed")
    season = season_of(boot)
    new_season_cleanup(season)
    dump(boot, "bootstrap.json")
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    team_names = {t["id"]: t["name"] for t in boot["teams"]}
    cur, nxt = current_and_next(boot["events"])
    cur_gw = cur["id"] if cur else None
    nxt_gw = nxt["id"] if nxt else None

    players = []
    for e in boot["elements"]:
        row = {c: e.get(c) for c in PLAYER_COLS}
        row["team"] = teams.get(e["team"], e["team"])
        row["team_name"] = team_names.get(e["team"], "")
        row["pos"] = POS.get(e["element_type"], e["element_type"])
        row["price"] = e["now_cost"] / 10.0
        players.append(row)
    write_csv(players, "players.csv", PLAYER_COLS + ["team_name", "price"])
    id2name = {e["id"]: e["web_name"] for e in boot["elements"]}
    id2team = {e["id"]: teams.get(e["team"]) for e in boot["elements"]}
    id2pos = {e["id"]: POS.get(e["element_type"]) for e in boot["elements"]}

    print("fixtures ...")
    fx = get("fixtures/")
    if fx is None:
        sys.exit("fixtures unavailable; keeping the previous data")
    dump(fx, "fixtures.json")
    fxrows = []
    for f in fx:
        fxrows.append({
            "id": f["id"], "event": f.get("event"), "kickoff_time": f.get("kickoff_time"),
            "team_h": teams.get(f["team_h"]), "team_a": teams.get(f["team_a"]),
            "team_h_difficulty": f.get("team_h_difficulty"), "team_a_difficulty": f.get("team_a_difficulty"),
            "started": f.get("started"), "finished": f.get("finished"), "minutes": f.get("minutes"),
            "team_h_score": f.get("team_h_score"), "team_a_score": f.get("team_a_score"),
        })
    write_csv(fxrows, "fixtures.csv", list(fxrows[0].keys()) if fxrows else ["id"])

    print("event-status ...")
    dump(get("event-status/") or {}, "event_status.json")

    # The raw standings response is deliberately NOT saved: it carries the league's name, every
    # team name and every manager's real name, and its file name would carry the league id.
    # league.csv keeps what analysis needs, keyed by aliases. Earlier versions did save
    # it, so any copy left behind is removed here.
    for stale in OUT.glob("league_*.json"):
        stale.unlink()
    for stale in (OUT / "entries").glob("*"):           # folders named by real entry id (old layout)
        if stale.is_dir() and stale.name.isdigit():
            shutil.rmtree(stale)
    print("league standings ...")
    league = get(f"leagues-classic/{LEAGUE_ID}/standings/")
    standings = (league or {}).get("standings", {}).get("results", [])
    if not standings and league:
        # before FPL first calculates a new season's standings, members sit under new_entries
        standings = [{"entry": r["entry"], "rank": None, "last_rank": None, "total": 0, "event_total": 0}
                     for r in (league.get("new_entries") or {}).get("results", [])]
    if not standings:
        sys.exit("league standings unavailable; keeping the previous data rather than writing an empty league")

    entry_ids = []
    for s in standings:  # pick up anyone who joined since config was written
        if str(s["entry"]) not in entry_ids:
            entry_ids.append(str(s["entry"]))

    league_rows = []
    squads = {}
    # A finished gameweek's picks never change, so each is fetched once and kept. The current and
    # previous weeks are always refreshed: FPL rewrites multipliers when it processes auto-subs.
    always = {g for g in (cur_gw, (cur_gw or 1) - 1, nxt_gw) if g and g >= 1}
    for eid in entry_ids:
        a = alias(eid)                                    # the only form of the id that is written
        print(f"entry {a} ...")
        hist = get(f"entry/{eid}/history/")
        if hist:
            hist = scrub_history(hist)
            dump(hist, f"entries/{a}/history.json")
        tr = get(f"entry/{eid}/transfers/")
        if tr is not None:
            dump(scrub_transfers(tr, a), f"entries/{a}/transfers.json")
        picks_by_gw = {}
        for g in range(1, (nxt_gw or cur_gw or 0) + 1):
            f = OUT / f"entries/{a}/picks_gw{g}.json"
            if g not in always and f.exists():
                picks_by_gw[g] = json.loads(f.read_text(encoding="utf-8"))
                continue
            pk = get(f"entry/{eid}/event/{g}/picks/")
            if pk:
                pk = scrub_picks(pk, a)
                dump(pk, f"entries/{a}/picks_gw{g}.json")
                picks_by_gw[g] = pk
            elif f.exists():                              # refresh failed: keep the last good copy
                picks_by_gw[g] = json.loads(f.read_text(encoding="utf-8"))
        cur_pk = picks_by_gw.get(cur_gw) or {}
        if cur_pk.get("active_chip") == "freehit" and not picks_by_gw.get((cur_gw or 0) - 1):
            sys.exit("a Free Hit squad cannot be reverted without the previous week's picks; keeping the previous data")
        chips = [f"{CHIP_NAMES.get(c['name'], c['name'])}@GW{c['event']}" for c in (hist or {}).get("chips", [])]
        curr = (hist or {}).get("current", [])
        last = curr[-1] if curr else {}
        st = next((s for s in standings if str(s["entry"]) == eid), {})
        pk = picks_by_gw.get(cur_gw) or {}
        picks = pk.get("picks", [])
        cap = next((p["element"] for p in picks if p.get("is_captain")), None)
        vc = next((p["element"] for p in picks if p.get("is_vice_captain")), None)
        eff, reverted = effective_squad(picks_by_gw, cur_gw)
        squads[a] = eff
        label = lambda i: f"{id2name.get(i, i)} ({id2team.get(i, '?')})"   # web_name alone is not unique
        squad = ", ".join(label(i) for i in eff)
        league_rows.append({
            "rank": st.get("rank"), "last_rank": st.get("last_rank"), "entry": a,
            # Deliberately NOT written: entry_name and player_name. Real people's names do not
            # belong in a public repo, and team names make the league searchable. Map aliases to
            # names privately (e.g. a names.local.json that never leaves your machine).
            "total": st.get("total"), "event_total": st.get("event_total"),
            "chips_used": " ".join(chips), "chips_left": " ".join(chips_left((hist or {}).get("chips", []), nxt_gw or cur_gw)),
            "bank": (last.get("bank") or 0) / 10.0, "value": (last.get("value") or 0) / 10.0,
            "gw_transfers": last.get("event_transfers"), "gw_hit": last.get("event_transfers_cost"),
            "bench_points": last.get("points_on_bench"),
            "active_chip": pk.get("active_chip"), "captain": label(cap) if cap else "", "vice": label(vc) if vc else "",
            "autosubs_processed": bool(pk.get("automatic_subs")),
            "squad_gw": nxt_gw or cur_gw, "free_hit_reverted": reverted,
            "squad_ids": ";".join(str(i) for i in eff), "squad": squad,
        })
    league_rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0))
    write_csv(league_rows, "league.csv", list(league_rows[0].keys()) if league_rows else ["entry"])

    # ownership across the league, from the squads carrying into the next GW (Free Hits reverted)
    own = {}
    for eid, sq in squads.items():
        for el in sq:
            own.setdefault(el, []).append(eid)        # alias, never a real id or a team name
    own_rows = [{"id": k, "web_name": id2name.get(k), "team": id2team.get(k), "pos": id2pos.get(k), "owners": len(v), "managers": "; ".join(v)}
                for k, v in sorted(own.items(), key=lambda kv: -len(kv[1]))]
    write_csv(own_rows, "ownership.csv", ["id", "web_name", "team", "pos", "owners", "managers"])

    if cur_gw:
        print(f"live gw{cur_gw} ...")
        live = get(f"event/{cur_gw}/live/")
        if live:
            dump(live, f"live_gw{cur_gw}.json")
            lrows = []
            for el in live.get("elements", []):
                s = el.get("stats", {})
                lrows.append({"id": el["id"], "web_name": id2name.get(el["id"]), "team": id2team.get(el["id"]), "pos": id2pos.get(el["id"]),
                              **{k: s.get(k) for k in ("minutes", "total_points", "goals_scored", "assists", "clean_sheets", "goals_conceded", "bonus", "bps", "yellow_cards", "red_cards", "saves", "defensive_contribution", "expected_goals", "expected_assists", "expected_goals_conceded")}})
            write_csv(lrows, f"live_gw{cur_gw}.csv", list(lrows[0].keys()) if lrows else ["id"])

    # daily price snapshot (kept small): one file per UTC date, overwritten through the day
    snap = [{"id": p["id"], "web_name": p["web_name"], "team": p["team"], "pos": p["pos"], "now_cost": p["now_cost"],
             "cost_change_event": p["cost_change_event"], "selected_by_percent": p["selected_by_percent"],
             "transfers_in_event": p["transfers_in_event"], "transfers_out_event": p["transfers_out_event"]} for p in players]
    day = started.strftime("%Y-%m-%d")
    write_csv(snap, f"../prices/{day}.csv", list(snap[0].keys()))
    pdir = ROOT / "data" / "prices"
    (pdir / "index.txt").write_text("\n".join(sorted(f.name for f in pdir.glob("*.csv"))) + "\n", encoding="utf-8")

    meta = {
        "pulled_at_utc": started.isoformat(timespec="seconds"),
        "current_gw": cur_gw, "next_gw": nxt_gw,
        "next_deadline_utc": (nxt or {}).get("deadline_time"),
        "current_deadline_utc": (cur or {}).get("deadline_time"),
        "current_finished": (cur or {}).get("finished"),
        "current_fixtures_finished": bool(cur_gw) and bool([f for f in fx if f.get("event") == cur_gw])
                                     and all(f.get("finished") for f in fx if f.get("event") == cur_gw),
        "season": season,
        "entries": [alias(e) for e in entry_ids],
    }
    dump(meta, "meta.json")
    manifest = sorted(str(p.relative_to(OUT)) for p in OUT.rglob("*") if p.is_file() and p.name != "manifest.txt")
    (OUT / "manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(f"done: gw{cur_gw}, {len(players)} players, {len(league_rows)} entries, {len(manifest)} files")


def main_elements():
    boot_p = OUT / "bootstrap.json"
    boot = json.loads(boot_p.read_text(encoding="utf-8")) if boot_p.exists() else get("bootstrap-static/")
    if not boot:
        sys.exit("bootstrap unavailable")
    new_season_cleanup(season_of(boot))
    min_min = int(CFG.get("element_summary_min_minutes", 1))
    ids = [e["id"] for e in boot["elements"] if (e.get("minutes") or 0) >= min_min]
    print(f"element summaries for {len(ids)} players ...")
    rows, missing = [], 0
    for i, pid in enumerate(ids, 1):
        js = get(f"element-summary/{pid}/")
        cached = OUT / f"elements/{pid}.json"
        if js:
            dump(js, f"elements/{pid}.json")
        elif cached.exists():                             # fetch failed: last good copy
            js = json.loads(cached.read_text(encoding="utf-8"))
        else:
            missing += 1
            continue
        for h in js.get("history", []):
            rows.append({"id": pid, **{k: h.get(k) for k in ("round", "opponent_team", "was_home", "minutes", "total_points", "goals_scored", "assists",
                                                             "clean_sheets", "goals_conceded", "bonus", "bps", "yellow_cards", "expected_goals", "expected_assists",
                                                             "expected_goal_involvements", "expected_goals_conceded", "defensive_contribution", "value", "selected", "transfers_in", "transfers_out")}})
        if i % 50 == 0:
            print(f"  {i}/{len(ids)}")
    hist_p = OUT / "player_gw_history.csv"
    if missing > max(3, 0.02 * len(ids)) and hist_p.exists():
        print(f"  ! {missing} players unavailable; keeping the previous player_gw_history.csv", file=sys.stderr)
    elif rows:
        if missing:
            print(f"  ! {missing} players unavailable and left out", file=sys.stderr)
        write_csv(rows, "player_gw_history.csv", list(rows[0].keys()))
    elif not ids and hist_p.exists():
        hist_p.unlink()                                   # new season, nobody has played yet
    manifest = sorted(str(p.relative_to(OUT)) for p in OUT.rglob("*") if p.is_file() and p.name != "manifest.txt")
    (OUT / "manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print("done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--elements", action="store_true", help="daily per-player history pull")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    main_elements() if a.elements else main_hourly()
