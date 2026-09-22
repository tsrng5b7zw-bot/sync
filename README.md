# Weekly data pull

A GitHub Actions job pulls the public Fantasy Premier League API every hour and commits the
results to `data/`. Nothing here needs a login, reads or writes a team, or touches any
copyrighted projection source.

## Setup

1. New **public** repository, any name.
2. Upload this folder's contents. If you drag-and-drop, reveal hidden files first
   (`Cmd+Shift+.` on macOS) or `.github/` is silently skipped and nothing ever runs. A web upload
   ignores `.gitignore`, so upload only the files listed here, never local working files.
3. **Settings → Secrets and variables → Actions → New repository secret**, twice:
   `FPL_LEAGUE_ID` (the league's number) and `FPL_ID_SALT` (any random string of 16+ characters;
   keep a copy, since it is what maps aliases back to teams).
4. **Settings → Actions → General → Workflow permissions → Read and write → Save.**
5. **Actions → Pull data → Run workflow.** Check `data/latest/` fills within a minute.

Runs hourly at :17 past, plus a daily 06:40 UTC job for per-player gameweek histories.

## What lands in `data/`

| File | Contents |
|---|---|
| `latest/players.csv` | every player: price, points, minutes, starts, xG, xA, xGC, defensive contributions, bonus, BPS, cards, penalty order, status, ownership, transfers, price change |
| `latest/fixtures.csv` | all fixtures with difficulty, kickoff times, scores |
| `latest/league.csv` | the mini-league by alias: rank, totals, chips used and left (both halves of the season), bank, value, captain, whether FPL has applied auto-subs, and each squad carrying into the next gameweek with Free Hits reverted |
| `latest/ownership.csv` | how many managers own each player, and which aliases |
| `latest/entries/<alias>/` | per-manager history, transfers, and picks for every gameweek (ranks removed) |
| `latest/live_gw<n>.csv` | live points while a gameweek runs |
| `latest/player_gw_history.csv` | per-player per-gameweek history (daily job) |
| `prices/<date>.csv` | daily price snapshot |
| `latest/meta.json` | pull time, current and next gameweek, deadlines, whether all matches are finished |

## Tools

```bash
python tools/fetch_latest.py owner/repo                 # download into ./data_pull
python tools/build_dataset.py --proj proj_raw.txt        # actuals, checked projections, league summary
python tools/optimize.py --squad my_squad.local.txt --bank 0.6 --me <alias> --transfers 1
python tools/rate_transfers.py --names names.local.json  # who transfers well, measured on real lineups
python tools/run_simulation.py --me <alias> --my-squad my_squad.local.txt --my-bank 0.6 \
       --committed WC --k-by auto --candidate alt.local.txt --names names.local.json
```

- **optimize.py** maximises projected points over the projection window: a legal XI and captain per
  gameweek, under budget and the 3-per-club cap. `--transfers N` limits changes and shows the gain
  over holding; `--horizon N` looks only N gameweeks ahead. The evidence gate stops a player
  *starting* (not being owned): for the whole window when both his points and his underlying
  numbers (xG, xA, xGC, defensive contributions) fall well short of the projection, and for the
  next gameweek only when FPL flags him injured, suspended or 0% to play; `--no-gate` turns it
  off. `--max-reserves N` caps non-playing bench fodder, since the optimiser does not value bench
  cover. Double and blank gameweeks are read from the fixtures. Players the projection source
  gives under a 50% chance to appear are weighted by that chance.
- **run_simulation.py** plays the season out 100,000 times with correlated scores (shared players,
  and teammates' clean sheets) calibrated to how spread out this league's weekly scores really are.
  A double gameweek widens that week's spread and a blank narrows it. A chip committed with
  `--committed` is scored in the week it is played.
  It reports the chance of each prize place and the money it is worth, in two separate blocks:
  a **forecast** (you and the rivals all keep transferring) and a **candidate comparison**
  (each squad scored as it stands). Rival skill is shown both ignored and tiered.
- **rate_transfers.py** scores every transfer window on the lineups actually played, with hits
  subtracted, Free Hits counted for one week and captaincy left out.

Players are keyed on FPL element ids (`web_name` alone is not unique) and managers on aliases.
`--me` takes your alias, or your real entry id when `FPL_ID_SALT` is set in the environment.

Projections are not collected — fplform's data is copyrighted, so it is read live in a browser
using `tools/fplform_snippets.md`. When no browser is available, blend three models that need none:

```bash
python tools/fetch_open_projections.py      # an open-source model's published projections (GitHub)
python tools/house_projections.py           # this repo's own model, from the collected FPL data
python tools/fetch_hgb_projections.py       # a second open-source model, gradient boosting (GitHub)
python tools/combine_projections.py proj_open.txt proj_house.txt proj_hgb.txt --weights 2,1,1
python tools/build_dataset.py --proj proj_blend.txt
```

The house and gradient-boosting models are both built from FPL's own data and agree closely, so
together they get the same weight as the open model. Averaged over GW4-5 of 2026/27 this blend
ranked players better than any single model, and ahead of FPL's own ep_next in both weeks. If a fetch fails, blend
the files you have. Nothing from either open model is stored in this repo; each is read at run
time, only as numbers, and a file that is stale, malformed, out of range or out of line with FPL's
ep_next is refused.

```bash
python tools/price_watch.py --squad my_squad.local.txt --watch "Name (TEAM);Name (TEAM)"
```

reads FPL's own price-change predictor (new in 2026/27, saved in `latest/bootstrap.json` by the
hourly pull) and says which players are expected to rise or fall at each of the next three updates.

## Keeping it unattributable

The committed data contains **no league name, no team names, no manager names and no FPL entry
ids**. Each manager appears as an alias (`m` + 8 hex characters) made with the `FPL_ID_SALT`
secret, so a rival who searches for their own team id finds nothing, and overall ranks and past
seasons are stripped. Error logs redact ids too, since a public repo's Actions logs are public.
Anything that would identify people stays on your machine, in files the `.gitignore` excludes:

- `*.local.json` — e.g. `names.local.json`, a map of aliases to display names for reports;
- `*.local.txt` — e.g. `my_squad.local.txt`. FPL hides your transfers and chips for the next
  deadline until it passes; a committed squad file would show rivals your team before then.

Leave the repository description and topics empty. The hourly job writes ~24 commits a day,
which shows on the account's activity graph, so host it on an account not connected to your name.
