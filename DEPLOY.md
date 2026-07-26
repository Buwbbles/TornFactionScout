# Putting this online

The whole site is one HTML file and one JSON file. That means it needs no server, no
database and no login — which also means visitors never hand over an API key, and you
never become responsible for anyone else's data.

**Your key crawls. Everyone else reads the results.** That single decision is what keeps
this cheap, fast and safe. Don't give it up without a reason.

Cost: nothing. Time: about twenty minutes, then it runs itself weekly.

---

## Step 1 — Get your key

In Torn: **Settings → API Key → Create new key**, access level **Public**. Name it
something like `FactionScout` so you can find and revoke it later.

Public access is all this needs. It reads faction pages and ranked war results, nothing
about you. If someone stole it they'd learn nothing they couldn't see by clicking around
the site.

Optional but worth it: register that same key at [ffscouter.com](https://ffscouter.com).
That's what fills in the battle-stat columns. Free.

---

## Step 2 — Make the repository

Create a [GitHub](https://github.com) account if you don't have one, then **New
repository**. Name it `faction-scout`. 

**Make it public.** Two reasons: GitHub Pages on a private repo needs a paid plan, and
public repos get unlimited Actions minutes. Your API key does *not* go in the repo — it
goes in Secrets, which stays private on a public repo.

Upload these files, keeping the folder structure:

```
faction-scout/
├── scout.py
├── dashboard.html
├── README.md
├── DEPLOY.md
└── .github/
    └── workflows/
        └── update.yml
```

Easiest way: **Add file → Upload files**, drag everything in, commit. For the workflow,
use **Add file → Create new file** and type the path `.github/workflows/update.yml`
into the filename box — GitHub creates the folders as you type the slashes.

Do **not** upload `config.json` or `scout.db`. The first would leak your key; the second
is rebuilt every run.

---

## Step 3 — Store the key as a secret

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TORN_API_KEY` | your Public key |
| `FFSCOUTER_KEY` | the same key, if you registered at FFScouter (optional) |

Secrets are write-only. Nobody can read them back out — including you, and including
anyone who forks the repo. GitHub deliberately withholds them from pull requests opened
by outsiders, so a stranger can't submit a PR that prints your key.

While you're in Settings → Actions → General, set **Fork pull request workflows** to
require approval. Belt and braces.

---

## Step 4 — Turn on Pages

**Settings → Pages → Source → GitHub Actions.**

Not "Deploy from a branch" — that's the older method and it won't work with this
workflow. There's nothing else to configure here.

---

## Step 5 — Run it

**Actions** tab → in the left sidebar pick **Refresh faction data and publish** → the
**Run workflow** button on the right → **Run workflow** in the dropdown.

That's the only workflow in the repo; there's nothing else to choose.

The first run takes **one to two hours**. It runs as separate steps so you can see where
it is — GitHub shows a live timer next to each:

| Step | Expect |
|---|---|
| Check the API is answering | seconds |
| Find factions | 2–6 minutes |
| Pull rosters | **30–60 minutes** |
| Pull stat estimates | 10–40 minutes, or seconds if you skipped FFScouter |
| Score, assemble, publish | 2–3 minutes |

"Pull rosters" prints a line every 25 factions with an elapsed time and an ETA. If you see
those ticking over, it's working — leave it alone.

When both jobs go green, your site is at:

```
https://YOUR-USERNAME.github.io/faction-scout/
```

From then on it refreshes every Monday at 04:10 UTC on its own.

---

## Step 6 — Check it actually worked

Open the site. In the left rail, under **Data on hand**, you should see four counts. What
they should look like after a real run:

| | Healthy | If it's low |
|---|---|---|
| rosters | within ~5% of the faction count | enrichment was cut off — check the log for rate-limit errors |
| war records | 1,000+ | the ranked war crawl failed; run `scout.py wars` locally to see the error |
| stat estimates | close to the roster count | no `FFSCOUTER_KEY`, or the key isn't registered at ffscouter.com |
| growth history | **0 on the first run** | expected — see below |

**Growth stays empty until the second week.** Respect-per-month can't be fetched from
anywhere; it has to be measured by comparing two readings taken apart in time. Your first
run is the baseline. Next Monday the column fills in, and it gets more accurate every week
after that as the history deepens.

---

## Tuning

**Refresh more often.** Edit the `cron` line in `update.yml`. `"10 4 * * *"` is daily.
Daily gives you sharper growth tracking, but weekly is plenty for choosing a faction and
uses a twentieth of the quota.

**Cover more factions.** In `config.json` defaults, raise `hof_pages` from 10 to 30, and
`war_history_days` from 240 to 400. Costs more time, finds more of the mid-tier.

**Finish sooner.** Stat estimates are the slowest step and only run for the top
`stats_faction_limit` factions by respect (1,500 by default). Drop it to 500 and that step
roughly triples in speed. Lower `hof_pages` to 5 and the roster pull shrinks too.

**Cover every faction.** The full ID sweep (60,000 IDs, ~11 hours at 90/min) won't fit in
a single Actions job. Run it locally instead:

```bash
python3 scout.py sweep           # resumable — Ctrl-C and rerun as often as you like
python3 scout.py history-save
```

Commit the resulting `history.json`. Every faction ID in it gets picked up by the next CI
run, so you only ever do the sweep once.

**Go faster.** Torn's limit is 100 calls a minute *per user*, shared across all of that
user's keys — so a second key of your own buys you nothing. A second key from a *different
player* genuinely doubles throughput. If a friend donates one, add it as `TORN_API_KEY_2`
and alternate. The per-IP ceiling is 1,000/min, so there's a lot of headroom.

**Custom domain.** Settings → Pages → Custom domain. Add a CNAME at your registrar
pointing to `YOUR-USERNAME.github.io`. Free HTTPS.

---

## What Torn's rules require of you

From Torn's API documentation, the obligations on a third-party service are about the
*end user's* key. Because this site never asks for one, the disclosure you owe is short,
and it's already written into the footer of `dashboard.html`: no key is collected, nothing
is stored, nothing is sent anywhere.

Two other things worth honouring:

- **No advertising.** Torn asks API-based sites not to run ads. Don't.
- **Say you're independent.** The footer already states the site isn't affiliated with
  Torn and that the data belongs to Torn.com. Leave that in.

If you ever add a feature that asks visitors for their key — a personal fit score, say —
those obligations change sharply. You'd then need a clearly visible ToS table stating what
you store, who can see it, why, and at what access level, wherever the key is entered. Do
that in the browser instead: keep the key in `localStorage`, call the API from the
visitor's own machine, never send it to your server. Then the honest disclosure is still
"we never receive it", which is the version you want to be able to make.

---

## When to outgrow this

Static hosting stops being enough when you want per-visitor state: saved shortlists,
accounts, alerts when a faction opens a slot, charts of a faction's respect over time
that don't ship the whole history to every visitor.

At that point the shape is a small VPS ($5/month), the same `scout.py` on cron writing to
Postgres instead of SQLite, and a thin API in front of it. Nothing about the collector or
the scoring changes — you'd be swapping the storage layer and adding a read API. Don't
build that until people are actually asking for it.

---

## When something breaks

**A step looks frozen** — check the timer next to the step name. If it's climbing, the job
is running. "Pull rosters" legitimately takes 30–60 minutes and there is nothing to do but
wait. Expand the step to see the progress lines and their ETAs.

If a step shows *no output at all* for more than a few minutes, that's the old buffering
bug: make sure `PYTHONUNBUFFERED: "1"` is still in the `env:` block near the top of
`update.yml`. Python hides its own output when CI captures it through a pipe, and a healthy
job then looks dead for an hour.

**Is it broken or just slow?** Run the diagnostic locally — it takes ten seconds and needs
nothing but your key in `config.json`:

```bash
python3 scout.py doctor
```

It calls every endpoint the crawler depends on and prints ok or FAIL for each, plus a time
estimate for the enrichment step. If every line says ok, the workflow isn't broken, it's
just long.

**Workflow fails immediately** — the secret name is misspelled, or Actions is disabled
under Settings → Actions → General. The Configure step now fails loudly with a named error
if `TORN_API_KEY` is empty, so check there first.

**Site 404s** — Pages source isn't set to "GitHub Actions", or the `publish` job hasn't
finished yet. Give it a minute.

**Site loads but shows the drop zone** — `factions.json` didn't make it into the artifact.
Check the "Assemble the site" step in the log.

**"Too many requests" in the log** — you were playing Torn on the same account during the
crawl. Harmless; the crawler backs off and retries. Lower `requests_per_minute` if it
happens a lot.

**Enrichment marks everything gone** — the API changed shape again. `scout.py` tries v1
then falls back to v2; if both fail, run `python3 scout.py enrich` locally and read the
error.
