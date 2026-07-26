# Faction Scout

Builds a local database of Torn factions from the official API and ranks them, so you can
pick one to join on evidence instead of recruitment-thread marketing.

Everything runs on your machine. Only your own API key is used, and only for reads.

---

## Setup

```bash
python3 scout.py init        # writes config.json
```

Open `config.json` and paste in a Torn API key. **A Public-access key is enough** — make one at
Settings → API Key → Public. Never hand a Limited or Full key to a tool you didn't write yourself,
including this one.

```bash
python3 scout.py demo        # optional: fake data so you can see the dashboard now
python3 scout.py score
open dashboard.html
```

When you're ready for real data:

```bash
python3 scout.py run         # discover → wars → enrich → stats → score
```

If `dashboard.html` shows a drop zone instead of the table, your browser blocked the local file
read. Either drag `factions.json` onto the drop zone, or serve the folder:

```bash
python3 -m http.server 8000  # then open localhost:8000/dashboard.html
```

---

## How long it takes

Torn allows **100 API calls per minute per user**, shared across every key you own — so extra keys
of your own don't help. The default is 55/min, leaving headroom to keep playing.

| Step | Calls | Time |
|---|---|---|
| Ranked war history (240 days) | ~15 | seconds |
| Hall of fame (10 pages × 3 categories) | ~30 | under a minute |
| Roster + basic for ~2,000 factions | ~2,000 | ~36 min |
| Full ID sweep, 1–60,000 (optional) | 60,000 | ~18 hours, resumable |

Start without the sweep. Hall of fame plus ranked-war history already covers essentially every
faction worth joining — a faction that has never warred and isn't in the top few thousand by
respect is not on your shortlist anyway.

---

## Commands

| | |
|---|---|
| `init` | write a starter config |
| `demo` | seed 140 synthetic factions to preview the UI |
| `discover` | find faction IDs from the hall of fame and war history |
| `wars` | crawl ranked war history into win/loss records |
| `enrich` | pull basic info and full roster for known factions |
| `stats` | pull battle-stat estimates from FFScouter |
| `score` | recompute metrics, write `factions.json` and `factions.csv` |
| `run` | all of the above |
| `snapshot` | force a fresh reading of every faction — for growth tracking |
| `prune` | drop stale rosters and compact the database |
| `history-save` | write `history.json` — the growth timeline, portable and tiny |
| `history-load` | merge `history.json` back in, so the database can be rebuilt anywhere |
| `status` | what's in the database |

Options: `--out DIR` sends `factions.json` and `factions.csv` somewhere else, `--slim` drops
rarely-read fields to shrink the payload, `--history FILE` points at a different history file.

---

## Growth tracking needs time

Respect-gained-per-month can't be pulled from the API. Nobody exposes it. The only honest way to
get it is to record respect now and compare later, which this does automatically — but your first
run only sets the baseline. The column stays empty until the second run.

Put this in cron and you'll have a real growth curve in a month:

```cron
0 4 * * 1  cd /path/to/faction-scout && /usr/bin/python3 scout.py snapshot && /usr/bin/python3 scout.py score
```

The scorer looks for the closest snapshot to 30 days ago and falls back to 60, 14, or 7 days,
reporting which window it used in `growth_window_days`.

---

## What the API will and won't tell you

**Available for any faction:** name, tag, respect, member count and capacity, faction age, rank,
best chain, whether they're enlisted for ranked war, and the full roster — every member's level,
days in faction, last action timestamp, and current status. Ranked war history with scores,
chains, and winners.

**Not available:** faction perks and upgrades. `faction/upgrades` requires Faction API Access
inside that faction, so you can only ever see your own. There's no workaround — anything claiming
otherwise is guessing.

What this tool does instead is put a **floor** under their upgrade spend. Chain milestones unlock
in a fixed order at published respect costs, and `best_chain` is public. A faction whose best chain
is 25,000 has necessarily bought every chain upgrade up to that tier. Combined with member
capacity, that gives `respect_invested_min` — a lower bound, labelled as one. To see the actual
perk loadout, ask in the application: any decent leader will screenshot their tree.

**Battle stats are private.** No API returns another player's stats. The estimates here come from
[FFScouter](https://ffscouter.com), which infers them from fair-fight values reported by thousands
of attackers. Their public estimates are explicitly free for anyone to use. Register your Torn key
there, put it in `config.json` as `ffscouter_key`, and the roster columns fill in.

Estimates are approximations. Above roughly 25b they get unreliable, and they're only as fresh as
the last time someone attacked that player. `stat_coverage` tells you what fraction of a roster
has an estimate at all — treat a faction under 0.6 with suspicion.

---

## The score

Eight components, each a percentile rank *within whatever you've filtered to*. Drag the sliders and
everything re-ranks live. The stacked bar on each row shows which components earned that faction
its score, in the slider colours.

| Component | Built from |
|---|---|
| War record | Adjusted 90-day win rate, all-time win rate, average score margin, current streak |
| Momentum | Respect per day, wars per month, days since last war |
| Muscle | Estimated stats: roster median, top-ten average, share of members over 1b |
| Activity | Share active in 7 days, share idle 30+ days |
| Development | Respect banked, chain tier unlocked |
| Stability | Faction age, median tenure, share who joined in the last 30 days |
| Openness | Free slots — a perfect faction you can't join scores zero here |
| Your fit | Where you'd land in their roster. Fill in `me` in `config.json` to enable |

**Win rates under five wars get pulled toward 50%.** A faction that is 2–0 is not a 100% faction,
and letting it top the table would make the whole ranking useless. The raw record is always shown
next to the adjusted rate so you can see what's underneath.

Missing data lands a faction at the median for that component, not the bottom. A faction with no
stat estimates isn't punished for it — but check `stat_coverage` before trusting its Muscle score.

Two presets: **Warmonger** (war record and momentum dominate) and **Steady** (activity, stability
and development dominate — for a faction that won't fold in three months).

---

## Reading the table honestly

A few things that matter more than they look:

- **Days since last war.** A faction with a great record that hasn't warred in 90 days has
  retired. Sort by this before you get excited about a win rate.
- **Idle 30d+.** The single best liveness signal. A 100-member faction where 40% haven't logged in
  for a month is a 60-member faction.
- **Bottom 10% stats.** Your actual question isn't "how strong is this faction" but "will I be
  carried or carrying". The median and the bottom decile answer that better than the total.
- **Joined in last 30 days.** High churn plus high win rate often means a mercenary roster that
  will scatter after the next war.
- **Open slots.** Filtered on by default. The strongest factions have none, and the ones with
  twenty open slots usually have them for a reason.

---

## Publishing it

See **DEPLOY.md**. The short version: your key crawls, everyone else reads a static file, and
GitHub Pages hosts it free on a weekly schedule. Visitors never enter an API key, which is what
keeps the whole thing simple and keeps you out of Torn's third-party data obligations.

---

## Rules

Torn permits API tools; it does not permit scraping the website or automating gameplay. This tool
only calls `api.torn.com` and `ffscouter.com`, only reads, and respects both rate limits. Keep it
that way if you modify it.

Related tools worth knowing about, so you don't rebuild them: **FFScouter** has faction comparison
and active ranked war pages, **TornStats** holds spy data if your faction shares it, and **TornPal**
tracks faction rosters and war history.
