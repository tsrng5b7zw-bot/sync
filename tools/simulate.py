"""Monte Carlo simulation of the mini-league: the chance of each finishing position and the
prize money that follows, rather than projected points.

An optimiser answers "how many points will this squad score"; a mini-league asks "how often do I
finish first, and what is that worth". The second depends on correlation. Managers who own the same
players score together, and a club's keeper and defenders win or lose clean sheets together, so the
model draws every manager's score jointly:

  window (the projection's gameweeks)
    Each manager re-optimises their squad with k good transfers (k = their skill; see
    rate_transfers.py), then plays the best XI and captain each week. Means come from the projections.
    Player weekly scores have a variance for their position x price band, measured from this
    season's gameweek history. Teammates are correlated by position pair (keeper/defenders strongly,
    through clean sheets), measured the same way. Manager covariance over the window:
        C = sum_g  W_g S W_g'        W_g: manager x player weights (1 starter, 2 captain)
                                     S:   player covariance
    C is then scaled so the model's week-to-week spread across managers equals the spread this
    league has actually produced (measured over every finished gameweek, chip weeks excluded).
    That scaling also absorbs noise the players cannot explain: auto-subs, benching errors, hits.

  after the window, to GW38
    Every manager scores the league average plus their window edge, decaying 11% a week
    (half-life about six gameweeks: squads converge as everyone chases the same form).
    Unused first-half chips are worth WC 12, BB 10, FH 10, TC 7 points each. Second-half chips are
    the same for everyone and cancel.

Limitations worth remembering when reading the numbers:
  * k is the most sensitive input. Report the uniform-k answer next to any tiered one.
  * Rival squads are whatever FPL shows publicly: pending transfers are invisible until a deadline.
  * No injuries, suspensions or rotation beyond what the projections already price in.
  * The after-window tail is an assumption, not data; it is kept deliberately simple.
"""
import numpy as np

from fplcommon import solve_squad, band, DEFAULT_VAR

CHIP_EV = {"WC": 12.0, "BB": 10.0, "FH": 10.0, "TC": 7.0}


def player_cov(ids, proj, var_bands, club_corr, other_club=-0.008):
    """Covariance of weekly scores between the given players (same order as ids)."""
    R = proj.rows
    sd = np.array([np.sqrt(var_bands.get(band(R[i]["pos"], R[i]["cost"]),
                                         DEFAULT_VAR.get(band(R[i]["pos"], R[i]["cost"]), 9.0))) for i in ids])
    team = np.array([R[i]["team"] for i in ids])
    back = np.array([R[i]["pos"] in ("GK", "DEF") for i in ids])
    same = team[:, None] == team[None, :]
    both_back = back[:, None] & back[None, :]
    one_back = back[:, None] ^ back[None, :]
    rho = np.where(same, np.where(both_back, club_corr["dd"], np.where(one_back, club_corr["da"], club_corr["aa"])), other_club)
    np.fill_diagonal(rho, 1.0)
    return rho * np.outer(sd, sd)


def window_moments(sols, order, proj, var_bands, club_corr):
    """Mean window points per manager and the (unscaled) covariance of their window totals.
    Returns (mu, C, C1): C scales each player's weekly spread by the square root of his club's
    matches that week (0 in a blank, 2 in a double); C1 treats every week as one match, which is
    what the league's measured spread describes, so calibration uses C1."""
    gws = proj.gws
    ids = sorted({i for e in order for g in gws for i in sols[e]["weights"][g]})
    ix = {p: n for n, p in enumerate(ids)}
    S = player_cov(ids, proj, var_bands, club_corr)
    C = np.zeros((len(order), len(order)))
    C1 = np.zeros((len(order), len(order)))
    for g in gws:
        W = np.zeros((len(order), len(ids)))
        for m, e in enumerate(order):
            for i, w in sols[e]["weights"][g].items():
                W[m, ix[i]] = w
        d = np.sqrt(np.array([proj.fixtures(i, g) for i in ids], float))
        C1 += W @ S @ W.T
        C += W @ (S * np.outer(d, d)) @ W.T
    mu = np.array([sols[e]["total"] for e in order])
    return mu, C, C1


def calibrate(C, mu, n_weeks, target_sd, rng, draws=20000):
    """Scale C so the expected cross-sectional SD of one week's scores (mean + noise) matches the
    league's measured spread. Returns (scale factor, model SD before scaling)."""
    Cw = C / n_weeks
    L = np.linalg.cholesky(Cw + 1e-9 * np.eye(len(Cw)))
    z = rng.standard_normal((draws, len(Cw))) @ L.T
    m = mu / n_weeks

    def spread(f):
        return float((m + np.sqrt(f) * z).std(axis=1, ddof=1).mean())

    lo, hi = 1e-3, 100.0
    for _ in range(60):
        mid = (lo * hi) ** 0.5
        lo, hi = (mid, hi) if spread(mid) < target_sd else (lo, mid)
    return (lo * hi) ** 0.5, spread(1.0)


def simulate(order, proj, squads, budgets, start, chips, k, me, var_bands, club_corr, sigma_week,
             prizes=(275, 125, 100, 50), nsim=100_000, decay=0.89, last_gw=38, seed=11, sols=None):
    """order: manager aliases; squads/budgets/start/chips: dicts by alias; k: dict alias -> transfers.
    Returns probabilities of every finishing position for everyone, and prize EV."""
    rng = np.random.default_rng(seed)
    gws = proj.gws
    sols = dict(sols or {})
    for e in order:
        if e not in sols:
            sols[e] = solve_squad(proj, budgets[e], base=squads[e], transfers=k.get(e, 0))
    mu, C, C1 = window_moments(sols, order, proj, var_bands, club_corr)
    G = len(gws)
    scale, model_sd = calibrate(C1, mu, G, sigma_week, rng)
    C, C1 = C * scale, C1 * scale
    after = max(0, last_gw - int(gws[-1][2:]))
    field = (mu / G).mean(); edge = mu / G - field
    tail = sum(decay ** t for t in range(after))
    base = (np.array([start[e] for e in order], float) + mu + field * after + edge * tail
            + np.array([chips[e] for e in order], float))
    L = np.linalg.cholesky(C + 1e-9 * np.eye(len(C)))
    if np.allclose(C, C1):                     # no blanks or doubles in the window
        noise = rng.standard_normal((nsim, len(order))) @ L.T * np.sqrt(1 + after / G)
    else:                                      # window as scheduled; the tail as ordinary weeks
        L1 = np.linalg.cholesky(C1 + 1e-9 * np.eye(len(C1)))
        noise = (rng.standard_normal((nsim, len(order))) @ L.T
                 + rng.standard_normal((nsim, len(order))) @ L1.T * np.sqrt(after / G))
    tot = base + noise
    rank = (-tot).argsort(1).argsort(1) + 1                    # 1 = top
    n = len(order)
    P = np.array([[(rank[:, j] == r).mean() for r in range(1, n + 1)] for j in range(n)])
    prize = np.zeros(n); prize[:len(prizes)] = prizes
    kme = order.index(me) if me in order else None
    out = {"order": order, "P": P, "ev": P @ prize, "mean_rank": P @ np.arange(1, n + 1),
           "window": dict(zip(order, mu)), "final_mean": dict(zip(order, base)),
           "scale": scale, "model_sd": model_sd, "sols": sols,
           "corr_with_me": None, "head_to_head": None}
    if kme is not None:
        sd = np.sqrt(np.diag(C))
        out["corr_with_me"] = {order[j]: float(C[kme, j] / (sd[kme] * sd[j])) for j in range(n) if j != kme}
        out["head_to_head"] = {order[j]: float((tot[:, kme] > tot[:, j]).mean()) for j in range(n) if j != kme}
    return out
