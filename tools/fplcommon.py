"""Shared helpers for the analysis tools. Players are keyed on FPL element ids, managers on aliases;
web_name is NOT unique (there are two Thomases, two Palmers, three Wilsons), so any name shown to
a person is "web_name (TEAM)".

These functions encode rules that were each got wrong at least once:
  * A Free Hit squad lasts one gameweek, then reverts to the previous week's squad.
  * Chips come in two sets: GW1-19 and GW20-38.
  * FPL applies auto-subs some hours after the last match. Until then the league table is
    missing them — for every manager, not just you.
  * Week-to-week noise must be measured over several gameweeks, not the latest one.
"""
import csv, hashlib, hmac, json, os, statistics as st
from collections import defaultdict
from pathlib import Path

CHIP_NAMES = {"wildcard": "WC", "freehit": "FH", "bboost": "BB", "3xc": "TC"}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_json(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def alias(entry_id, salt):
    """The collector's stand-in for an FPL entry id (see collector/pull.py)."""
    return "m" + hmac.new(salt.encode(), str(entry_id).encode(), hashlib.sha256).hexdigest()[:8]


class Data:
    """Everything under a fetched data directory (the output of tools/fetch_latest.py).
    Managers appear only as aliases ('m' + 8 hex); see collector/pull.py."""

    def __init__(self, root="data_pull"):
        self.root = Path(root)
        self.D = self.root / "latest"
        if not (self.D / "meta.json").exists():
            raise SystemExit(f"no {self.D}/meta.json — run tools/fetch_latest.py first")
        self.meta = read_json(self.D / "meta.json")
        self.cur_gw = self.meta.get("current_gw")
        self.nxt_gw = self.meta.get("next_gw")
        self.players = {int(r["id"]): r for r in read_csv(self.D / "players.csv")}
        self.league = read_csv(self.D / "league.csv") if (self.D / "league.csv").exists() else []
        self._round_pts = None

    def resolve_entry(self, value):
        """Accept an alias as it appears in the data, or a real entry id when FPL_ID_SALT is set."""
        if value is None:
            return None
        v = str(value).strip()
        known = {str(r["entry"]) for r in self.league}
        if v in known:
            return v
        salt = os.environ.get("FPL_ID_SALT", "")
        if v.isdigit() and salt and alias(v, salt) in known:
            return alias(v, salt)
        raise SystemExit(f"entry {v!r} is not in the league data (pass the alias, or the real id with FPL_ID_SALT set)")

    # ---- players -------------------------------------------------------------------------
    def label(self, el):
        p = self.players.get(int(el))
        return f"{p['web_name']} ({p['team']})" if p else str(el)

    def round_points(self):
        """{(element, round): (points, minutes)}, summed over fixtures so double gameweeks
        count both matches rather than the second overwriting the first."""
        if self._round_pts is None:
            acc = defaultdict(lambda: [0, 0])
            path = self.D / "player_gw_history.csv"
            if path.exists():
                for r in read_csv(path):
                    k = (int(r["id"]), int(r["round"]))
                    acc[k][0] += int(float(r["total_points"] or 0))
                    acc[k][1] += int(float(r["minutes"] or 0))
            self._round_pts = {k: tuple(v) for k, v in acc.items()}
        return self._round_pts

    # ---- entries --------------------------------------------------------------------------
    def picks(self, eid, gw):
        return read_json(self.D / "entries" / str(eid) / f"picks_gw{gw}.json")

    def history(self, eid):
        return read_json(self.D / "entries" / str(eid) / "history.json") or {}

    def transfers(self, eid):
        return read_json(self.D / "entries" / str(eid) / "transfers.json") or []

    def squad(self, eid):
        """Element ids of the squad carrying into the next gameweek, Free Hits reverted.
        Prefers the collector's own squad_ids column; falls back to the picks files."""
        row = next((r for r in self.league if str(r["entry"]) == str(eid)), None)
        if row and row.get("squad_ids"):
            return [int(x) for x in row["squad_ids"].split(";") if x]
        cur = self.picks(eid, self.cur_gw) or {}
        if cur.get("active_chip") == "freehit":
            prev = self.picks(eid, self.cur_gw - 1)
            if prev:
                return [p["element"] for p in prev["picks"]]
        return [p["element"] for p in cur.get("picks", [])]

    def selling_prices(self, eid, ids):
        """What each player would sell for today: purchase price plus half of any rise, rounded
        down to 0.1m; the current price if he has fallen. Purchase prices come from the public
        transfer history (latest purchase wins); players held since the start were bought at their
        opening price. Anyone not in that history - a move made for a deadline that has not passed
        yet - is taken at today's price."""
        bought = {}
        for t in sorted(self.transfers(eid), key=lambda t: (t["event"], t.get("time") or "")):
            bought[t["element_in"]] = t["element_in_cost"]
        public = set(self.squad(eid))
        out = {}
        for el in ids:
            p = self.players[el]
            now = int(p["now_cost"])
            if el in bought:
                buy = bought[el]
            elif el in public:
                buy = now - int(p["cost_change_start"] or 0)
            else:
                buy = now
            out[el] = (buy + (now - buy) // 2 if now > buy else now) / 10
        return out

    def chips_left(self, eid, committed=()):
        """Chips still available for the next gameweek, both sets handled. `committed` lists
        chips already locked in but not yet recorded — FPL only logs a chip once its gameweek's
        deadline passes, so a Wildcard played for next week still shows as unused."""
        gw = self.nxt_gw or self.cur_gw or 1
        first = gw <= 19
        used = {CHIP_NAMES.get(c["name"], c["name"]) for c in self.history(eid).get("chips", [])
                if (c["event"] <= 19) == first}
        return sorted({"WC", "FH", "BB", "TC"} - used - set(committed))

    # ---- corrections ----------------------------------------------------------------------
    def last_finished_gw(self):
        """The latest gameweek whose matches are all over (the current one, or the one before)."""
        return self.cur_gw if self.fixtures_finished() else (self.cur_gw or 1) - 1

    def fixtures_finished(self):
        if "current_fixtures_finished" in self.meta:
            return bool(self.meta["current_fixtures_finished"])
        fx = [r for r in read_csv(self.D / "fixtures.csv") if str(r.get("event")) == str(self.cur_gw)]
        return bool(fx) and all(str(r.get("finished")) == "True" for r in fx)

    def pending_autosubs(self, eid):
        """Points FPL still owes this manager for auto-subs in the current gameweek. Zero once FPL
        has processed them (picks carry automatic_subs), or while matches are still being played."""
        pk = self.picks(eid, self.cur_gw)
        if not pk or pk.get("automatic_subs") or not self.fixtures_finished():
            return 0
        live = {}
        lp = self.D / f"live_gw{self.cur_gw}.csv"
        if lp.exists():
            for r in read_csv(lp):
                live[int(r["id"])] = (int(float(r["total_points"] or 0)), int(float(r["minutes"] or 0)))
        else:
            live = {k[0]: v for k, v in self.round_points().items() if k[1] == self.cur_gw}
        pos = lambda el: self.players[el]["pos"] if el in self.players else "?"
        picks = sorted(pk["picks"], key=lambda p: p["position"])
        xi = [p["element"] for p in picks if p["position"] <= 11]
        bench = [p["element"] for p in picks if p["position"] > 11]
        mult = {p["element"]: p["multiplier"] for p in picks}
        played = lambda el: live.get(el, (0, 0))[1] > 0
        boost = pk.get("active_chip") == "bboost"     # all fifteen score: no bench subs, armband rules still apply

        def legal(team):
            c = {k: sum(1 for e in team if pos(e) == k) for k in ("GK", "DEF", "MID", "FWD")}
            return c["GK"] == 1 and c["DEF"] >= 3 and c["MID"] >= 2 and c["FWD"] >= 1

        team, used, gain = list(xi), set(), 0
        for el in ([] if boost else xi):
            if played(el):
                continue
            for b in bench:
                if b in used or not played(b) or (pos(b) == "GK") != (pos(el) == "GK"):
                    continue
                trial = [b if e == el else e for e in team]
                if legal(trial):
                    team = trial; used.add(b); gain += live[b][0]
                    break
        cap = next((p for p in picks if p["is_captain"]), None)
        vc = next((p for p in picks if p["is_vice_captain"]), None)
        if cap and not played(cap["element"]) and vc and vc["element"] in team and played(vc["element"]):
            gain += live[vc["element"]][0] * (mult[cap["element"]] - 1)   # vice inherits the armband
        return gain

    def sigma(self):
        """Typical spread of weekly scores across the league, net of hits, averaged over every
        finished gameweek. Managers on Bench Boost or Triple Captain that week are excluded,
        since a chip week is not ordinary noise."""
        per, boosted = defaultdict(list), defaultdict(set)
        last = self.last_finished_gw()
        for r in self.league:
            eid = str(r["entry"]); h = self.history(eid)
            for c in h.get("chips", []):
                if c["name"] in ("bboost", "3xc"):
                    boosted[c["event"]].add(eid)
            for x in h.get("current", []):
                if x["event"] <= last:                 # a gameweek still being played is not noise yet
                    per[x["event"]].append((eid, x["points"] - x.get("event_transfers_cost", 0)))
        sds = []
        for g, rows in per.items():
            v = [p for e, p in rows if e not in boosted[g]]
            if len(v) >= 4:
                sds.append(st.stdev(v))
        return (st.mean(sds), len(sds)) if sds else (11.7, 0)


# ---- player-level variance and club correlation, estimated from the gameweek history ---------

def band(pos, price):
    if pos == "GK":
        return "GK"
    return f"{pos} " + ("<5.0" if price < 5.0 else "5-7.5" if price < 7.5 else "7.5+")


DEFAULT_VAR = {"GK": 8.8, "DEF <5.0": 12.9, "DEF 5-7.5": 8.6, "DEF 7.5+": 10.0, "MID <5.0": 1.9,
               "MID 5-7.5": 8.7, "MID 7.5+": 28.6, "FWD <5.0": 9.0, "FWD 5-7.5": 12.4, "FWD 7.5+": 14.7}
DEFAULT_CLUBCORR = {"dd": 0.59, "da": 0.11, "aa": 0.12}


def variance_bands(data, min_games=3, min_df=15):
    """Pooled within-player week-to-week variance by position x price band."""
    games = defaultdict(list)
    for (el, _), (pts, mins) in data.round_points().items():
        if mins >= 60:
            games[el].append(pts)
    acc = defaultdict(lambda: [0.0, 0])
    for el, v in games.items():
        if len(v) < min_games or el not in data.players:
            continue
        p = data.players[el]; m = st.mean(v)
        b = band(p["pos"], float(p["price"]))
        acc[b][0] += sum((x - m) ** 2 for x in v); acc[b][1] += len(v) - 1
    out = dict(DEFAULT_VAR)
    for b, (ss, df) in acc.items():
        if df >= min_df:
            out[b] = ss / df
    return out


def club_correlation(data, min_games=4):
    """Average correlation of weekly scores between teammates, by position pair. Clean sheets
    bind a club's keeper and defenders together; attackers are close to independent."""
    series = defaultdict(dict)
    for (el, g), (pts, mins) in data.round_points().items():
        if mins >= 60:
            series[el][g] = pts
    z = {}
    for el, v in series.items():
        if len(v) < min_games or el not in data.players:
            continue
        m = st.mean(v.values()); s = st.pstdev(v.values())
        if s >= 0.5:
            z[el] = {g: (x - m) / s for g, x in v.items()}
    by_club = defaultdict(list)
    for el in z:
        by_club[data.players[el]["team"]].append(el)
    acc = defaultdict(list)
    D = {"GK", "DEF"}
    for members in by_club.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                common = set(z[a]) & set(z[b])
                if len(common) < min_games:
                    continue
                r = sum(z[a][g] * z[b][g] for g in common) / len(common)
                pa, pb = data.players[a]["pos"], data.players[b]["pos"]
                key = "dd" if (pa in D and pb in D) else "da" if (pa in D or pb in D) else "aa"
                acc[key].append(r)
    out = dict(DEFAULT_CLUBCORR)
    for k, v in acc.items():
        if len(v) >= 30:
            out[k] = st.mean(v)
    return out


# ---- projections and squad files -------------------------------------------------------------

class Projections:
    """A projection table (pipe-delimited, one row per player, a column per gameweek: gw6, gw7 ...)
    joined to FPL element ids on (web_name, team). Prices come from the FPL data, not the file,
    because the file's prices go stale the moment a price changes.

    Players that a squad needs but the file lacks (it only lists players worth projecting) are
    added with zero projections by `ensure`, so a squad is never silently short of 15."""

    def __init__(self, path, data, horizon=None):
        self.data = data
        lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
        head = lines[0].split("|")
        gws = sorted((h for h in head if h[:2] == "gw" and h[2:].isdigit()), key=lambda h: int(h[2:]))
        if not gws:
            raise SystemExit(f"{path}: no gw<n> columns in header {head}")
        nxt = data.nxt_gw or 0
        self.dropped = [g for g in gws if int(g[2:]) < nxt]       # already locked in: cannot be changed
        gws = [g for g in gws if int(g[2:]) >= nxt]
        if not gws:
            raise SystemExit(f"{path}: every projected gameweek is before GW{nxt}; fetch fresh projections")
        self.gap = int(gws[0][2:]) > nxt if nxt else False           # projections skip the next gameweek
        self.gws = gws[:horizon] if horizon else gws
        self.rows, self.unmatched = {}, []
        by_key = {(p["web_name"], p["team"]): el for el, p in data.players.items()}
        for ln in lines[1:]:
            d = dict(zip(head, ln.split("|")))
            el = by_key.get((d.get("name"), d.get("team")))
            if el is None:
                self.unmatched.append(f"{d.get('name')} ({d.get('team')})")
                continue
            try:
                gw = {g: float(d.get(g) or 0) for g in self.gws}
            except ValueError:
                self.unmatched.append(f"{d.get('name')} ({d.get('team')}) [bad number]")
                continue
            self.rows[el] = self._row(el, gw, d)

    def _row(self, el, gw, d=None):
        p = self.data.players[el]
        d = d or {}
        return {"id": el, "name": p["web_name"], "team": p["team"], "pos": p["pos"],
                "cost": float(p["price"]), "gw": gw, "next": sum(gw.values()),
                "ros": float(d.get("ros") or 0), "news": d.get("news", ""), "projected": bool(d)}

    def ensure(self, ids):
        added = []
        for el in ids:
            if el not in self.rows and el in self.data.players:
                self.rows[el] = self._row(el, {g: 0.0 for g in self.gws})
                added.append(el)
        return added

    def label(self, el):
        return self.data.label(el)


def read_squad_file(path, data):
    """Squad file: one player per line as 'web_name (TEAM)' or a bare element id; '#' starts a
    comment. Returns element ids and exits on anything it cannot resolve, since a squad that is
    quietly short of a player produces confident nonsense."""
    by_key = {f"{p['web_name']} ({p['team']})": el for el, p in data.players.items()}
    ids, bad = [], []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        s = ln.split("#", 1)[0].strip()
        if not s:
            continue
        if s.isdigit() and int(s) in data.players:
            ids.append(int(s))
        elif s in by_key:
            ids.append(by_key[s])
        else:
            bad.append(s)
    if bad:
        raise SystemExit(f"{path}: cannot resolve {bad} — use 'web_name (TEAM)' exactly as players.csv has it, or the element id")
    if len(ids) != 15 or len(set(ids)) != 15:
        raise SystemExit(f"{path}: need 15 distinct players, got {len(set(ids))}")
    pos = [data.players[e]["pos"] for e in ids]
    need = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
    if any(pos.count(k) != v for k, v in need.items()):
        raise SystemExit(f"{path}: squad must be 2 GK / 5 DEF / 5 MID / 3 FWD, got "
                         + " / ".join(f"{pos.count(k)} {k}" for k in need))
    return ids


def write_squad_file(path, ids, data, header=""):
    order = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
    ids = sorted(ids, key=lambda e: (order.get(data.players[e]["pos"], 9), data.players[e]["web_name"]))
    text = (f"# {header}\n" if header else "") + "\n".join(data.label(e) for e in ids) + "\n"
    Path(path).write_text(text, encoding="utf-8")


# ---- squad optimiser (shared by optimize.py and simulate.py) --------------------------------

SQUAD_SHAPE = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}


def solve_squad(proj, budget, base=None, transfers=None, must=(), ban=(), no_start=(),
                max_club=3, ros_weight=0.0, timeout=120, sell=None):
    """Pick 15 players and a legal XI + captain for each gameweek in proj.gws, maximising
    projected points (captain counted twice). With `base` and `transfers`, at least
    15 - transfers of the base squad are kept. `sell` gives owned players' selling prices, which
    is what keeping them costs; everyone else costs today's price, and `budget` should then be
    bank + selling value. `no_start` players count zero points (they can still fill a slot if
    nothing else can). Returns dict(status, squad, lineups, caps, total, cost, weights)."""
    import pulp
    gws = proj.gws
    base = list(base or [])
    proj.ensure(base + list(must))
    P = list(proj.rows)
    R = proj.rows
    sell = sell or {}
    cost = {i: sell.get(i, R[i]["cost"]) if i in base else R[i]["cost"] for i in P}
    pts = {(i, g): 0.0 if i in no_start else R[i]["gw"][g] for i in P for g in gws}
    m = pulp.LpProblem("squad", pulp.LpMaximize)
    x = {i: pulp.LpVariable(f"x{i}", cat="Binary") for i in P}
    y = {(i, g): pulp.LpVariable(f"y{i}_{g}", cat="Binary") for i in P for g in gws}
    c = {(i, g): pulp.LpVariable(f"c{i}_{g}", cat="Binary") for i in P for g in gws}
    obj = pulp.lpSum((y[i, g] + c[i, g]) * pts[i, g] for i in P for g in gws)
    if ros_weight:
        obj += ros_weight * pulp.lpSum(x[i] * R[i]["ros"] for i in P)
    m += obj
    m += pulp.lpSum(x.values()) == 15
    for pos, n in SQUAD_SHAPE.items():
        m += pulp.lpSum(x[i] for i in P if R[i]["pos"] == pos) == n
    m += pulp.lpSum(x[i] * cost[i] for i in P) <= budget + 1e-6
    for t in {R[i]["team"] for i in P}:
        m += pulp.lpSum(x[i] for i in P if R[i]["team"] == t) <= max_club
    for g in gws:
        m += pulp.lpSum(y[i, g] for i in P) == 11
        for pos, n in XI_MIN.items():
            e = pulp.lpSum(y[i, g] for i in P if R[i]["pos"] == pos)
            m += (e == n) if pos == "GK" else (e >= n)
        m += pulp.lpSum(c[i, g] for i in P) == 1
        for i in P:
            m += y[i, g] <= x[i]
            m += c[i, g] <= y[i, g]
    for i in must:
        m += x[i] == 1
    for i in ban:
        if i in x:
            m += x[i] == 0
    if base and transfers is not None:
        m += pulp.lpSum(x[i] for i in base) >= len(base) - transfers
    m.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=timeout))
    status = pulp.LpStatus[m.status]
    if status != "Optimal":
        raise RuntimeError(f"solver: {status} (budget {budget:.1f}, transfers {transfers}) — infeasible or timed out")
    on = lambda v: v.value() is not None and v.value() > 0.5
    squad = [i for i in P if on(x[i])]
    lineups = {g: [i for i in P if on(y[i, g])] for g in gws}
    caps = {g: next(i for i in P if on(c[i, g])) for g in gws}
    weights = {g: {i: (2.0 if caps[g] == i else 1.0) for i in lineups[g]} for g in gws}
    total = sum(pts[i, g] * w for g in gws for i, w in weights[g].items())
    return {"status": status, "squad": squad, "lineups": lineups, "caps": caps, "weights": weights,
            "total": total, "cost": sum(cost[i] for i in squad)}
