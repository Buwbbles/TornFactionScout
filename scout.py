#!/usr/bin/env python3
"""
Torn Faction Scout
==================
Builds a local database of every faction it can find in Torn, then ranks them
so you can pick one to join.

Everything here uses the official Torn API (api.torn.com) and the public
FFScouter estimate API. No page scraping, no automation of gameplay.

Quick start:
    python3 scout.py init
    # put your Torn API key in config.json (Public access is enough)
    python3 scout.py run
    # open dashboard.html

Commands:
    init      write a starter config.json
    demo      seed synthetic data so you can see the dashboard immediately
    discover  find faction IDs (hall of fame + ranked war history)
    wars      crawl ranked war history and build win/loss records
    enrich    pull basic + roster for known factions
    stats     pull battle-stat estimates from FFScouter (optional)
    score     compute metrics and write factions.json / factions.csv
    run       discover -> wars -> enrich -> stats -> score
    snapshot  record a point-in-time respect reading (for growth tracking)
    status    show what's in the database
"""

import argparse
import csv
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "scout.db")
CONFIG_PATH = os.path.join(HERE, "config.json")
USER_AGENT = "TornFactionScout/1.0 (personal faction research tool)"
OPTS = {}
CFG = {"enrich": {}}

DEFAULT_CONFIG = {
    "torn_api_key": "",
    "_comment_key": "A Public-access key is enough. Never paste a Limited/Full key into a tool you didn't write.",
    "ffscouter_key": "",
    "_comment_ffscouter": "Optional. Your Torn key registered at ffscouter.com. Unlocks battle-stat estimates.",
    "requests_per_minute": 55,
    "_comment_rate": "Torn allows 100/min per USER across all your keys. 55 leaves headroom for playing.",
    "me": {
        "level": 0,
        "battle_stats_total": 0,
        "_comment": "Used for the personal Fit score. Fill in or leave at 0 to disable."
    },
    "discovery": {
        "hof_pages": 30,
        "_comment_hof": "Each page = 100 factions, fetched for 3 categories. 30 pages costs ~90 calls and finds most active factions.",
        "war_history_days": 400,
        "id_sweep": {
            "enabled": False,
            "start": 1,
            "end": None,
            "_comment_end": "null auto-detects from the highest faction ID actually seen.",
            "_comment": "Brute-force every faction ID. Thorough but slow: ~18 hours at 55/min."
        }
    },
    "enrich": {
        "refresh_after_hours": 20,
        "max_per_run": 100000,
        "live_within_days": 7,
        "quiet_within_days": 90,
        "dead_after_days": 365,
        "_comment_tiers": "A faction is tiered by the last thing anyone in it did. Anything past dead_after_days is kept in the database but left out of the published rankings.",
        "refresh_days_by_tier": {"live": 0, "quiet": 7, "dormant": 30, "abandoned": 90},
        "stats_faction_limit": 1500,
        "_comment_stats": "Only look up battle-stat estimates for the top N factions by respect. This is the slowest step; lower it to finish sooner."
    }
}


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

class RateLimiter:
    """Simple spacing limiter. Torn's limit is per-user, not per-key."""

    def __init__(self, per_minute):
        self.interval = 60.0 / max(1, per_minute)
        self.last = 0.0

    def wait(self):
        gap = time.time() - self.last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self.last = time.time()


class TornClient:
    def __init__(self, key, limiter, verbose=True):
        self.key = key
        self.limiter = limiter
        self.verbose = verbose
        self.calls = 0

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        for attempt in range(4):
            self.limiter.wait()   # every attempt counts against the limit
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.calls += 1
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt * 3)
                    continue
                body = e.read().decode("utf-8", "replace")[:200]
                raise RuntimeError("HTTP %s: %s" % (e.code, body))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(2 ** attempt * 3)
        raise RuntimeError("gave up on %s" % url.split("?")[0])

    # Torn error 6 = incorrect ID. Anything else is transient or our fault.
    ERR_NO_SUCH_ID = 6

    def call(self, path, version=1, **params):
        """Returns (data, error_dict_or_None). Torn returns 200 with an error body."""
        params["key"] = self.key
        params["comment"] = "FactionScout"
        base = "https://api.torn.com/" if version == 1 else "https://api.torn.com/v2/"
        url = base + path.lstrip("/") + "?" + urllib.parse.urlencode(params)
        try:
            data = self._get(url)
        except RuntimeError as e:
            return None, {"error": str(e), "code": -1}
        if isinstance(data, dict) and "error" in data and isinstance(data["error"], dict):
            return None, data["error"]
        return data, None


def _progress(done, total, t0):
    """Progress with an ETA. A long-running step needs to prove it is alive."""
    elapsed = time.time() - t0
    pct = 100.0 * done / max(1, total)
    if done and elapsed > 1:
        eta = elapsed / done * (total - done)
        return "%d/%d (%.0f%%)  %s elapsed, ~%s left" % (
            done, total, pct, _dur(elapsed), _dur(eta))
    return "%d/%d (%.0f%%)" % (done, total, pct)


def _dur(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds // 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS faction (
        id INTEGER PRIMARY KEY, name TEXT, tag TEXT,
        first_seen INTEGER, source TEXT, last_enriched INTEGER, dead INTEGER DEFAULT 0,
        tier TEXT, last_activity INTEGER
    );
    CREATE TABLE IF NOT EXISTS snapshot (
        faction_id INTEGER, ts INTEGER, respect INTEGER, member_count INTEGER,
        capacity INTEGER, best_chain INTEGER, rank_name TEXT, rank_level INTEGER,
        rank_position INTEGER, rank_wins INTEGER, age_days INTEGER,
        leader INTEGER, co_leader INTEGER, enlisted INTEGER,
        PRIMARY KEY (faction_id, ts)
    );
    CREATE TABLE IF NOT EXISTS member (
        faction_id INTEGER, player_id INTEGER, ts INTEGER, name TEXT, level INTEGER,
        days_in_faction INTEGER, last_action_ts INTEGER, status_state TEXT, position TEXT,
        PRIMARY KEY (faction_id, player_id, ts)
    );
    CREATE TABLE IF NOT EXISTS player_stat (
        player_id INTEGER PRIMARY KEY, ts INTEGER, bs_estimate INTEGER,
        bss_public INTEGER, fair_fight REAL, estimate_age INTEGER, source TEXT
    );
    CREATE TABLE IF NOT EXISTS war (
        war_id INTEGER PRIMARY KEY, start_ts INTEGER, end_ts INTEGER, winner INTEGER, target INTEGER
    );
    CREATE TABLE IF NOT EXISTS war_side (
        war_id INTEGER, faction_id INTEGER, name TEXT, score INTEGER, chain INTEGER,
        PRIMARY KEY (war_id, faction_id)
    );
    CREATE TABLE IF NOT EXISTS territory (
        name TEXT PRIMARY KEY, faction_id INTEGER, size INTEGER, daily_respect INTEGER
    );
    CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
    CREATE INDEX IF NOT EXISTS idx_snap_f ON snapshot(faction_id, ts);
    CREATE INDEX IF NOT EXISTS idx_mem_f ON member(faction_id, ts);
    CREATE INDEX IF NOT EXISTS idx_side_f ON war_side(faction_id);
    CREATE INDEX IF NOT EXISTS idx_terr_f ON territory(faction_id);
    """)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """CREATE TABLE IF NOT EXISTS silently skips new columns on an existing
    database, so upgrading in place would crash on the first UPDATE. Add anything
    missing by hand."""
    wanted = {
        "faction": [("tier", "TEXT"), ("last_activity", "INTEGER")],
    }
    for table, cols in wanted.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in cols:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                print("Database upgraded: added %s.%s" % (table, name))


def kv_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, str(v)))
    conn.commit()


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("No config.json. Run: python3 scout.py init")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def note_faction(conn, fid, name=None, tag=None, source="unknown"):
    conn.execute(
        "INSERT OR IGNORE INTO faction (id,name,tag,first_seen,source) VALUES (?,?,?,?,?)",
        (fid, name, tag, int(time.time()), source))
    if name:
        conn.execute("UPDATE faction SET name=COALESCE(?,name), tag=COALESCE(?,tag) WHERE id=?",
                     (name, tag, fid))


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def cmd_discover(conn, cfg, client):
    before = conn.execute("SELECT COUNT(*) c FROM faction").fetchone()["c"]

    # Source 1: faction hall of fame. Ranked by respect, so it lands the
    # established factions first. v2 only.
    pages = cfg["discovery"].get("hof_pages", 10)
    print("Hall of fame: %d pages per category (%d factions max)" % (pages, pages * 100))
    for cat in ("respect", "chain", "chains"):
        empty_streak = 0
        for page in range(pages):
            data, err = client.call("torn/factionhof", version=2,
                                    cat=cat, limit=100, offset=page * 100)
            if err:
                print("  %s: %s" % (cat, err.get("error")))
                break
            rows = _unwrap_hof(data)
            if not rows:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            for r in rows:
                fid = r.get("id") or r.get("faction_id")
                if fid:
                    note_faction(conn, int(fid), r.get("name"), None, "hof:" + cat)
            conn.commit()
            print("  %s page %d: +%d" % (cat, page + 1, len(rows)))

    # Source 2: ranked war history. Anyone who has warred recently is, by
    # definition, the population you care about.
    cmd_wars(conn, cfg, client)

    # Source 3 (optional): sweep the ID space. Slow but complete.
    sweep = cfg["discovery"].get("id_sweep", {})
    if sweep.get("enabled"):
        _id_sweep(conn, client, sweep["start"], sweep["end"])

    after = conn.execute("SELECT COUNT(*) c FROM faction").fetchone()["c"]
    print("Known factions: %d (+%d)" % (after, after - before))


def _unwrap_hof(data):
    if not isinstance(data, dict):
        return []
    for key in ("factionhof", "factionhof_list", "hof", "factions"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            out = []
            for k, item in v.items():
                if isinstance(item, dict):
                    item.setdefault("id", k)
                    out.append(item)
            return out
    return []


def _id_sweep(conn, client, start, end):
    global CFG
    cursor = int(kv_get(conn, "sweep_cursor", start))
    hits, t0, span = 0, time.time(), max(1, end - cursor)
    for fid in range(cursor, end + 1):
        data, err = client.call("faction/%d" % fid, version=1, selections="basic")
        if not err and isinstance(data, dict) and data.get("name"):
            note_faction(conn, fid, data.get("name"), data.get("tag"), "sweep")
            _store_basic(conn, fid, data)
            _classify(conn, fid, CFG)
            hits += 1
        if fid % 200 == 0:
            kv_set(conn, "sweep_cursor", fid)
            conn.commit()
            print("  %s  %d factions found" % (_progress(fid - cursor, span, t0), hits))
    kv_set(conn, "sweep_cursor", end)
    conn.commit()


# --------------------------------------------------------------------------
# ranked wars
# --------------------------------------------------------------------------

def _detect_ceiling(conn):
    """The highest faction ID actually in circulation, plus headroom. Hardcoding a
    ceiling silently misses the newest factions, which are the ones recruiting."""
    top = 0
    for q in ("SELECT MAX(id) m FROM faction",
              "SELECT MAX(faction_id) m FROM war_side",
              "SELECT MAX(faction_id) m FROM territory"):
        try:
            top = max(top, conn.execute(q).fetchone()["m"] or 0)
        except sqlite3.Error:
            pass
    ceiling = int(top * 1.15) + 2000 if top else 60000
    print("Highest live faction ID seen: %d. Sweeping to %d for headroom." % (top, ceiling))
    return ceiling


def cmd_sweep(conn, cfg, client):
    """Walk every faction ID in existence. This is the only way to get a truly
    complete list — the hall of fame is capped and ranked wars only show factions
    that enlist. Slow, but resumable: stop and restart it as often as you like."""
    sweep = cfg["discovery"].get("id_sweep", {})
    start = sweep.get("start", 1)
    end = sweep.get("end") or _detect_ceiling(conn)
    rate = cfg.get("requests_per_minute", 55)
    cursor = int(kv_get(conn, "sweep_cursor", start))
    remaining = max(0, end - cursor)
    print("Sweeping IDs %d-%d. %d left, about %s at %d calls/min."
          % (cursor, end, remaining, _dur(remaining * 60.0 / rate), rate))
    print("Safe to interrupt — progress is saved every 200 IDs.")
    _id_sweep(conn, client, cursor, end)


def cmd_wars(conn, cfg, client):
    """Build real ranked war history.

    torn/rankedwars only returns the *current* war pool — matchmaking runs once a
    week, so every faction in it shows exactly one war. That endpoint is for
    discovering war IDs, not history. Actual results live in torn/rankedwarreport,
    one call per war ID, which is public. War IDs are sequential, so we walk them.

    Resumable and incremental: the first run backfills, later runs only fetch wars
    that appeared since.
    """
    days = cfg["discovery"].get("war_history_days", 400)
    now = int(time.time())
    cutoff = now - days * DAY

    # Newest war IDs come from the live pool.
    newest = 0
    data, err = client.call("torn/", version=1, selections="rankedwars")
    if not err:
        for wid, w in ((data or {}).get("rankedwars") or {}).items():
            try:
                newest = max(newest, int(wid))
            except (TypeError, ValueError):
                pass
            _store_war(conn, wid, w.get("war") or {}, w.get("factions") or {})
        conn.commit()
    row = conn.execute("SELECT MAX(war_id) m, MIN(war_id) n FROM war").fetchone()
    have_max, have_min = row["m"] or 0, row["n"] or 0
    newest = max(newest, have_max)

    if not newest:
        print("  could not find any war IDs to start from — is the key working?")
        return

    print("Ranked wars: walking reports back %d days from war #%d" % (days, newest))
    fetched = misses = 0
    t0 = time.time()
    oldest_seen = None

    # Forward pass first (cheap, catches anything new), then backward backfill.
    ranges = []
    if have_max and newest > have_max:
        ranges.append(range(have_max + 1, newest + 1))
    ranges.append(range(have_min - 1 if have_min else newest, 0, -1))

    stop = False
    for rng in ranges:
        if stop:
            break
        consecutive_misses = 0
        for wid in rng:
            if wid <= 0:
                break
            if conn.execute("SELECT 1 FROM war WHERE war_id=?", (wid,)).fetchone():
                continue
            d, e = client.call("torn/%d" % wid, version=1, selections="rankedwarreport")
            report = (d or {}).get("rankedwarreport") if isinstance(d, dict) else None
            if e or not report:
                misses += 1
                consecutive_misses += 1
                # Gaps in the ID space are normal; a long run of them means we've
                # walked off the end of the sequence.
                if consecutive_misses >= 40:
                    break
                continue
            consecutive_misses = 0
            war = report.get("war") or {}
            _store_war(conn, wid, war, report.get("factions") or {})
            fetched += 1
            ts = int(war.get("end") or war.get("start") or 0)
            if ts:
                oldest_seen = ts if oldest_seen is None else min(oldest_seen, ts)
            if fetched % 50 == 0:
                conn.commit()
                age = "" if not oldest_seen else "  back to %s" % time.strftime(
                    "%Y-%m-%d", time.gmtime(oldest_seen))
                print("  %d wars fetched, %d gaps, %s elapsed%s"
                      % (fetched, misses, _dur(time.time() - t0), age))
            if ts and ts < cutoff:
                print("  reached the %d day cutoff" % days)
                stop = True
                break
        conn.commit()

    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM war").fetchone()["c"]
    sides = conn.execute("SELECT COUNT(DISTINCT faction_id) c FROM war_side").fetchone()["c"]
    print("  %d wars on record across %d factions (+%d this run)" % (n, sides, fetched))


def _store_war(conn, war_id, war, factions):
    try:
        war_id = int(war_id)
    except (TypeError, ValueError):
        return
    winner = war.get("winner")
    try:
        winner = int(winner) if winner else None
    except (TypeError, ValueError):
        winner = None
    conn.execute("INSERT OR REPLACE INTO war VALUES (?,?,?,?,?)", (
        war_id, int(war.get("start") or 0), int(war.get("end") or 0),
        winner, war.get("target")))

    if isinstance(factions, list):
        factions = {f.get("id"): f for f in factions if isinstance(f, dict) and f.get("id")}
    for fid, f in (factions or {}).items():
        if not isinstance(f, dict):
            continue
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        conn.execute("INSERT OR REPLACE INTO war_side VALUES (?,?,?,?,?)",
                     (war_id, fid, f.get("name"),
                      int(f.get("score") or 0), int(f.get("chain") or 0)))
        note_faction(conn, fid, f.get("name"), None, "rankedwar")


def cmd_territory(conn, cfg, client):
    """One call returns every territory in the game and who holds it. Territories
    pay daily respect, so this is a cheap read on how organised a faction is."""
    data, err = client.call("torn/", version=1, selections="territory")
    terr = (data or {}).get("territory") if isinstance(data, dict) else None
    if err or not terr:
        print("Territory: unavailable (%s)" % (err.get("error") if err else "empty response"))
        return
    conn.execute("DELETE FROM territory")
    n = 0
    for name, t in terr.items():
        if not isinstance(t, dict):
            continue
        fid = t.get("faction") or 0
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        if not fid:
            continue
        conn.execute("INSERT OR REPLACE INTO territory VALUES (?,?,?,?)",
                     (name, fid, int(t.get("size") or 0), int(t.get("daily_respect") or 0)))
        note_faction(conn, fid, source="territory")
        n += 1
    conn.commit()
    holders = conn.execute("SELECT COUNT(DISTINCT faction_id) c FROM territory").fetchone()["c"]
    print("Territory: %d held across %d factions" % (n, holders))


# --------------------------------------------------------------------------
# enrichment
# --------------------------------------------------------------------------

def cmd_enrich(conn, cfg, client):
    """Re-reads factions, but not all of them equally often. A faction nobody has
    touched in a year does not need a weekly roster pull; one that warred on
    Tuesday does. Without this, a full directory makes every run take hours."""
    hours = cfg["enrich"].get("refresh_after_hours", 20)
    limit = cfg["enrich"].get("max_per_run", 100000)
    now = int(time.time())
    tiers = cfg["enrich"].get("refresh_days_by_tier", {"live": 0, "quiet": 7, "dormant": 30})
    rows = conn.execute("""
        SELECT f.id,
               COALESCE(f.tier, 'live') AS tier,
               COALESCE(f.last_enriched, 0) AS le
        FROM faction f WHERE f.dead = 0
    """).fetchall()
    due = []
    for r in rows:
        wait = tiers.get(r["tier"], 0) * DAY + hours * 3600
        if now - r["le"] >= wait:
            due.append(r["id"])
    due = due[:limit]
    counts = {}
    for r in rows:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    if counts:
        print("Tiers: " + ", ".join("%s %d" % kv for kv in sorted(counts.items())))
    rows = [{"id": i} for i in due]
    print("Enriching %d factions (~%s at current rate)" %
          (len(rows), _dur(len(rows) * client.limiter.interval)))

    ok = gone = failed = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        fid = r["id"]
        data, err = client.call("faction/%d" % fid, version=1, selections="basic")
        if err or not isinstance(data, dict) or not data.get("name"):
            # v2 fallback: basic and members are separate endpoints there
            definitely_gone = bool(err) and err.get("code") == TornClient.ERR_NO_SUCH_ID
            data = None if definitely_gone else _v2_basic(client, fid)
            if not data:
                if definitely_gone:
                    # Only retire a faction when Torn says the ID doesn't exist.
                    # A timeout or a 500 must not delete a live faction.
                    conn.execute("UPDATE faction SET dead=1, last_enriched=? WHERE id=?",
                                 (int(time.time()), fid))
                    gone += 1
                else:
                    failed += 1
                continue
        _store_basic(conn, fid, data)
        _classify(conn, fid, cfg)
        ok += 1
        if i % 25 == 0:
            conn.commit()
            print("  %s  %d ok, %d retired, %d failed" %
                  (_progress(i, len(rows), t0), ok, gone, failed))
    conn.commit()
    print("  updated %d, retired %d, temporarily failed %d" % (ok, gone, failed))
    if failed:
        print("  (failures are retried next run — nothing was discarded)")


def _classify(conn, fid, cfg):
    """Tier a faction by the most recent thing anyone in it did. This is the only
    activity signal available on first contact — respect growth and member churn
    both need two crawls, and waiting a week to find out a faction is dead is no
    use when you are building a directory."""
    row = conn.execute("""
        SELECT MAX(last_action_ts) la, COUNT(*) n FROM member
        WHERE faction_id=? AND ts=(SELECT MAX(ts) FROM member WHERE faction_id=?)
    """, (fid, fid)).fetchone()
    last = row["la"] or 0
    war = conn.execute("SELECT MAX(w.end_ts) e FROM war_side s JOIN war w USING(war_id) "
                       "WHERE s.faction_id=?", (fid,)).fetchone()["e"] or 0
    last = max(last, war)
    age = (int(time.time()) - last) / DAY if last else 99999
    live = cfg["enrich"].get("live_within_days", 7)
    quiet = cfg["enrich"].get("quiet_within_days", 90)
    dead = cfg["enrich"].get("dead_after_days", 365)
    tier = "live" if age <= live else "quiet" if age <= quiet else \
           "dormant" if age <= dead else "abandoned"
    conn.execute("UPDATE faction SET tier=?, last_activity=? WHERE id=?", (tier, last, fid))


def _v2_basic(client, fid):
    basic, err = client.call("faction/%d/basic" % fid, version=2)
    if err or not isinstance(basic, dict):
        return None
    b = basic.get("basic") or basic
    if not b.get("name"):
        return None
    members, merr = client.call("faction/%d/members" % fid, version=2)
    if not merr and isinstance(members, dict):
        mlist = members.get("members")
        if isinstance(mlist, list):
            b["members"] = {str(m.get("id")): m for m in mlist if m.get("id")}
        elif isinstance(mlist, dict):
            b["members"] = mlist
    return b


def _store_basic(conn, fid, d):
    ts = int(time.time())
    rank = d.get("rank") or {}
    if not isinstance(rank, dict):
        rank = {}
    members = d.get("members") or {}
    if isinstance(members, list):
        members = {str(m.get("id")): m for m in members if isinstance(m, dict)}

    member_count = d.get("members_count")
    if member_count is None:
        member_count = len(members) if members else 0

    # v1 exposes currently-scheduled ranked wars; v2 has an explicit flag. The old
    # fallback also counted hall-of-fame position, which nearly every faction has,
    # so almost everything read as enlisted.
    enlisted = d.get("is_enlisted")
    if enlisted is None:
        enlisted = 1 if d.get("ranked_wars") else 0

    conn.execute("INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        fid, ts,
        int(d.get("respect") or 0), int(member_count or 0),
        int(d.get("capacity") or 0), int(d.get("best_chain") or 0),
        rank.get("name"), int(rank.get("level") or 0),
        int(rank.get("position") or 0), int(rank.get("wins") or 0),
        int(d.get("age") or 0),
        int(d.get("leader") or d.get("leader_id") or 0),
        int(d.get("co-leader") or d.get("co_leader") or d.get("coleader") or 0),
        1 if enlisted else 0,
    ))

    for pid, m in members.items():
        if not isinstance(m, dict):
            continue
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        la = m.get("last_action") or {}
        la_ts = la.get("timestamp") if isinstance(la, dict) else la
        st = m.get("status") or {}
        state = st.get("state") if isinstance(st, dict) else st
        conn.execute("INSERT OR REPLACE INTO member VALUES (?,?,?,?,?,?,?,?,?)", (
            fid, pid, ts, m.get("name"), int(m.get("level") or 0),
            int(m.get("days_in_faction") or 0), int(la_ts or 0), state,
            m.get("position")))

    conn.execute("UPDATE faction SET name=?, tag=?, last_enriched=?, dead=0 WHERE id=?",
                 (d.get("name"), d.get("tag"), ts, fid))


def cmd_snapshot(conn, cfg, client):
    """Force a fresh reading for every known faction, ignoring the refresh window.
    Run this weekly on a cron to build real growth history."""
    cfg = dict(cfg)
    cfg["enrich"] = dict(cfg["enrich"])
    cfg["enrich"]["refresh_after_hours"] = 0
    cmd_enrich(conn, cfg, client)


# --------------------------------------------------------------------------
# battle stat estimates (FFScouter)
# --------------------------------------------------------------------------

def cmd_stats(conn, cfg, client):
    key = (cfg.get("ffscouter_key") or "").strip()
    if not key:
        print("No ffscouter_key in config. Skipping stat estimates.")
        print("Register your Torn key at https://ffscouter.com to enable this.")
        return

    # Stat estimates are the slowest step by far, and a dead six-member faction
    # doesn't need them. Restrict to factions you might actually join.
    cap = cfg.get("enrich", {}).get("stats_faction_limit", 1500)
    latest = conn.execute("""
        SELECT DISTINCT m.player_id FROM member m
        JOIN (SELECT faction_id, MAX(ts) mts FROM member GROUP BY faction_id) x
          ON m.faction_id = x.faction_id AND m.ts = x.mts
        WHERE m.faction_id IN (
            SELECT s.faction_id FROM snapshot s
            JOIN (SELECT faction_id, MAX(ts) mts FROM snapshot GROUP BY faction_id) y
              ON s.faction_id = y.faction_id AND s.ts = y.mts
            ORDER BY s.respect DESC LIMIT ?)
    """, (cap,)).fetchall()
    stale = int(time.time()) - 14 * 86400
    have = {r["player_id"] for r in conn.execute(
        "SELECT player_id FROM player_stat WHERE ts > ?", (stale,)).fetchall()}
    todo = [r["player_id"] for r in latest if r["player_id"] not in have]

    print("Stat estimates: %d players to look up (%d cached)" % (len(todo), len(have)))
    limiter = RateLimiter(18)  # FFScouter allows 20/min per IP
    now = int(time.time())
    t0 = time.time()

    for i in range(0, len(todo), 200):
        chunk = todo[i:i + 200]
        limiter.wait()
        url = ("https://ffscouter.com/api/v1/get-stats?"
               + urllib.parse.urlencode({"key": key, "targets": ",".join(map(str, chunk))}))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print("  batch failed: %s" % e)
            continue
        if isinstance(data, dict) and "error" in data:
            print("  FFScouter: %s" % data.get("error"))
            return
        for p in data or []:
            if not isinstance(p, dict):
                continue
            conn.execute("INSERT OR REPLACE INTO player_stat VALUES (?,?,?,?,?,?,?)", (
                int(p["player_id"]), now,
                int(p.get("bs_estimate") or 0), int(p.get("bss_public") or 0),
                float(p.get("fair_fight") or 0), int(p.get("last_updated") or 0),
                p.get("source")))
        conn.commit()
        print("  %s" % _progress(min(i + 200, len(todo)), len(todo), t0))


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

DAY = 86400


def cmd_factiontree(conn, cfg, client):
    """Pull Torn's own upgrade tree so the chain-cost table isn't my guesswork.
    Cached in the database; a few hundred bytes and it rarely changes."""
    data, err = client.call("torn/", version=1, selections="factiontree")
    tree = (data or {}).get("factiontree") if isinstance(data, dict) else None
    if err or not tree:
        print("Faction tree: unavailable (%s). Falling back to the built-in table."
              % (err.get("error") if err else "empty"))
        return
    kv_set(conn, "factiontree", json.dumps(tree))
    branches = len(tree) if isinstance(tree, (dict, list)) else 0
    print("Faction tree: cached %d branches — chain costs now come from Torn." % branches)


def _chain_costs(conn):
    """Cumulative respect to unlock each chain tier. Prefers Torn's live tree."""
    raw = kv_get(conn, "factiontree")
    if raw:
        try:
            tree = json.loads(raw)
            found = []
            def walk(node):
                if isinstance(node, dict):
                    name = str(node.get("name", ""))
                    ability = str(node.get("ability", ""))
                    blob = (name + " " + ability).lower()
                    if "chain" in blob:
                        import re as _re
                        nums = _re.findall(r"(\d[\d,]*)", blob)
                        cost = node.get("challenge") or node.get("base_price") or node.get("price")
                        if nums and cost:
                            found.append((int(nums[-1].replace(",", "")), int(cost)))
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            walk(tree)
            if len(found) >= 5:
                found.sort()
                total, out = 0, []
                for size, cost in found:
                    total += cost
                    out.append((size, total))
                return out, "torn/factiontree"
        except (ValueError, TypeError, KeyError):
            pass
    return CHAIN_TIERS, "built-in table (Torn's tree was unavailable)"


def cmd_score(conn, cfg, _client=None):
    now = int(time.time())
    me = cfg.get("me", {})

    dead_after = cfg.get("enrich", {}).get("dead_after_days", 365)
    factions = {}
    excluded = 0
    for r in conn.execute("SELECT * FROM faction WHERE dead=0").fetchall():
        tier = r["tier"] or "live"
        if tier == "abandoned":
            excluded += 1
            continue
        factions[r["id"]] = {"id": r["id"], "name": r["name"], "tag": r["tag"],
                             "tier": tier, "last_activity": r["last_activity"] or 0}

    territory = {}
    for r in conn.execute("SELECT faction_id, COUNT(*) n, SUM(daily_respect) dr "
                          "FROM territory GROUP BY faction_id").fetchall():
        territory[r["faction_id"]] = (r["n"], r["dr"] or 0)

    stats_pool = {r["faction_id"] for r in conn.execute(
        "SELECT DISTINCT faction_id FROM member m WHERE EXISTS "
        "(SELECT 1 FROM player_stat p WHERE p.player_id = m.player_id)").fetchall()}

    # latest + historical snapshots
    snaps = defaultdict(list)
    for r in conn.execute("SELECT * FROM snapshot ORDER BY ts").fetchall():
        snaps[r["faction_id"]].append(dict(r))

    # war record
    war_rows = conn.execute("""
        SELECT ws.faction_id, ws.war_id, ws.score, ws.chain, w.start_ts, w.end_ts, w.winner
        FROM war_side ws JOIN war w ON w.war_id = ws.war_id
        WHERE w.end_ts > 0
    """).fetchall()
    opponent_score = {}
    for r in conn.execute("SELECT war_id, faction_id, score FROM war_side").fetchall():
        opponent_score.setdefault(r["war_id"], {})[r["faction_id"]] = r["score"]

    wars = defaultdict(list)
    for r in war_rows:
        others = [v for k, v in opponent_score.get(r["war_id"], {}).items() if k != r["faction_id"]]
        wars[r["faction_id"]].append({
            "war_id": r["war_id"], "end": r["end_ts"], "score": r["score"] or 0,
            "chain": r["chain"] or 0, "won": 1 if r["winner"] == r["faction_id"] else 0,
            "opp_score": others[0] if others else 0,
        })

    # rosters (latest snapshot per faction)
    roster = defaultdict(list)
    for r in conn.execute("""
        SELECT m.* FROM member m
        JOIN (SELECT faction_id, MAX(ts) mts FROM member GROUP BY faction_id) x
          ON m.faction_id=x.faction_id AND m.ts=x.mts
    """).fetchall():
        roster[r["faction_id"]].append(dict(r))

    stats = {r["player_id"]: dict(r) for r in
             conn.execute("SELECT * FROM player_stat").fetchall()}

    chain_costs, chain_source = _chain_costs(conn)

    out = []
    for fid, base in factions.items():
        s = snaps.get(fid)
        if not s:
            continue
        cur = s[-1]
        rec = dict(base)
        rec.update({
            "respect": cur["respect"], "members": cur["member_count"],
            "capacity": cur["capacity"], "open_slots": max(0, (cur["capacity"] or 0) - (cur["member_count"] or 0)),
            "best_chain": cur["best_chain"], "rank_name": cur["rank_name"],
            "rank_level": cur["rank_level"], "age_days": cur["age_days"],
            "enlisted": cur["enlisted"], "updated": cur["ts"],
        })

        # --- growth: needs two snapshots. Report the best window we have. ---
        rec["respect_gain_30d"] = None
        rec["respect_gain_per_day"] = None
        rec["growth_window_days"] = None
        for target_days in (30, 60, 14, 7):
            past = _snapshot_near(s, now - target_days * DAY)
            if past and past["ts"] < cur["ts"] - 2 * DAY:
                span = (cur["ts"] - past["ts"]) / DAY
                delta = cur["respect"] - past["respect"]
                rec["respect_gain_per_day"] = round(delta / span, 1)
                rec["respect_gain_30d"] = round(delta / span * 30)
                rec["growth_window_days"] = round(span, 1)
                break

        # --- war record ---
        w = sorted(wars.get(fid, []), key=lambda x: x["end"], reverse=True)
        rec["wars_total"] = len(w)
        rec["wars_won"] = sum(x["won"] for x in w)
        rec["winrate_all"] = round(rec["wars_won"] / len(w), 3) if w else None
        for label, days in (("60d", 60), ("90d", 90), ("180d", 180)):
            recent = [x for x in w if x["end"] >= now - days * DAY]
            rec["wars_" + label] = len(recent)
            rec["wins_" + label] = sum(x["won"] for x in recent)
            rec["winrate_" + label] = round(sum(x["won"] for x in recent) / len(recent), 3) if recent else None
        recent90 = [x for x in w if x["end"] >= now - 90 * DAY]
        rec["wars_per_month"] = round(len(recent90) / 3.0, 2)
        margins = [x["score"] - x["opp_score"] for x in recent90 if x["opp_score"]]
        rec["avg_margin_90d"] = round(statistics.mean(margins)) if margins else None
        rec["avg_war_score_90d"] = round(statistics.mean([x["score"] for x in recent90])) if recent90 else None
        rec["last_war_ts"] = w[0]["end"] if w else None
        rec["days_since_war"] = round((now - w[0]["end"]) / DAY) if w else None
        streak = 0
        for x in w:
            if x["won"]:
                streak += 1
            else:
                break
        rec["win_streak"] = streak

        # --- roster health ---
        mem = roster.get(fid, [])
        rec["roster_size"] = len(mem)
        if mem:
            la = [m["last_action_ts"] for m in mem if m["last_action_ts"]]
            rec["pct_active_24h"] = round(sum(1 for t in la if now - t < DAY) / len(mem), 3) if la else None
            rec["pct_active_7d"] = round(sum(1 for t in la if now - t < 7 * DAY) / len(mem), 3) if la else None
            rec["pct_inactive_30d"] = round(sum(1 for t in la if now - t > 30 * DAY) / len(mem), 3) if la else None
            levels = [m["level"] for m in mem if m["level"]]
            rec["level_median"] = statistics.median(levels) if levels else None
            rec["level_min"] = min(levels) if levels else None
            rec["level_max"] = max(levels) if levels else None
            tenure = [m["days_in_faction"] for m in mem if m["days_in_faction"] is not None]
            rec["tenure_median"] = statistics.median(tenure) if tenure else None
            rec["pct_joined_30d"] = round(sum(1 for t in tenure if t <= 30) / len(tenure), 3) if tenure else None
            rec["pct_hospital"] = round(sum(1 for m in mem if m["status_state"] == "Hospital") / len(mem), 3)

            est = [stats[m["player_id"]]["bs_estimate"] for m in mem
                   if m["player_id"] in stats and stats[m["player_id"]]["bs_estimate"]]
            rec["stat_coverage"] = round(len(est) / len(mem), 2)
            if len(est) >= 5:
                est.sort()
                rec["stats_total"] = sum(est)
                rec["stats_median"] = est[len(est) // 2]
                rec["stats_min"] = est[0]
                rec["stats_p10"] = est[max(0, int(len(est) * 0.10))]
                rec["stats_max"] = est[-1]
                rec["stats_top10_avg"] = round(statistics.mean(est[-10:]))
                q = lambda p: est[min(len(est) - 1, int(len(est) * p))]
                rec["stats_q"] = [q(.10), q(.25), q(.50), q(.75), q(.90)]
                rec["wall_1b"] = sum(1 for e in est if e >= 1_000_000_000)
                rec["wall_10b"] = sum(1 for e in est if e >= 10_000_000_000)
                if me.get("battle_stats_total"):
                    rec["your_percentile"] = round(
                        sum(1 for e in est if e < me["battle_stats_total"]) / len(est), 2)
            else:
                for k in ("stats_total", "stats_median", "stats_min", "stats_p10",
                          "stats_max", "stats_top10_avg", "wall_1b", "wall_10b",
                          "stats_q", "your_percentile"):
                    rec[k] = None

        # --- development proxy: perks aren't public, so infer investment ---
        rec["respect_invested_min"] = _infer_investment(
            cur["best_chain"], cur["capacity"], chain_costs)
        rec["chain_tier"] = _chain_tier(cur["best_chain"])

        t = territory.get(fid)
        rec["territories"] = t[0] if t else 0
        rec["territory_respect_daily"] = t[1] if t else 0
        rec["days_since_activity"] = round((now - base["last_activity"]) / DAY) \
            if base["last_activity"] else None

        # Say plainly whether a blank means "no data exists" or "we didn't look".
        rec["stats_looked_up"] = fid in stats_pool
        rec["playstyle"] = _playstyle(rec)
        rec["torn_url"] = "https://www.torn.com/factions.php?step=profile&ID=%d" % fid
        out.append(rec)

    out.sort(key=lambda r: (r.get("winrate_90d") or 0, r.get("respect") or 0), reverse=True)

    is_demo = bool(conn.execute(
        "SELECT 1 FROM faction WHERE source='demo' LIMIT 1").fetchone())

    payload = {
        "demo": is_demo,
        "notes": {
            "respect_invested_min": (
                "A floor, not a real figure. Faction perks are private to their own "
                "members, so this is inferred from chain tier and member capacity, "
                "both of which are public and unlock in a fixed order at known "
                "respect costs. Chain costs come from %s. Capacity costs rise as a "
                "faction grows and this assumes a flat average per upgrade, so the "
                "real figure is higher than what you see here." % chain_source),
            "stats": (
                "Battle stats are private in Torn. These are public estimates from "
                "FFScouter, inferred from fair-fight values reported by attackers. "
                "They get unreliable above about 25b and are only as fresh as the "
                "last time someone attacked that player."),
            "stats_coverage": (
                "Estimates are only looked up for the top %d factions by respect, to "
                "keep collection within the API rate limit. A blank stat column on a "
                "smaller faction means it was not checked, not that the faction is "
                "weak." % cfg.get("enrich", {}).get("stats_faction_limit", 1500)),
            "growth": (
                "Respect growth is measured by comparing two readings taken apart in "
                "time. It is blank until this has run at least twice, and the window "
                "actually used is shown as growth_window_days."),
            "winrate": (
                "Records under five wars are pulled toward 50%%. A 2-0 faction is not "
                "a 100%% faction. Raw records are always shown alongside."),
            "activity": (
                "Factions where nobody has done anything in %d days are collected but "
                "left out of these rankings." % dead_after),
            "recruit": (
                "For your first 72 hours in any faction you are a Recruit: no faction "
                "perks, no chain contribution, no organised crimes, and no score in a "
                "ranked war. Joining the night before a war does not get you into it."),
        },
        "excluded_inactive": excluded,
        "generated": now,
        "generated_human": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)),
        "faction_count": len(out),
        "you": me,
        "coverage": {
            "with_roster": sum(1 for r in out if r.get("roster_size")),
            "with_war_record": sum(1 for r in out if r.get("wars_total")),
            "with_stat_estimates": sum(1 for r in out if r.get("stats_median")),
            "with_growth": sum(1 for r in out if r.get("respect_gain_30d") is not None),
            "with_territory": sum(1 for r in out if r.get("territories")),
            "by_tier": {t: sum(1 for r in out if r.get("tier") == t)
                        for t in ("live", "quiet", "dormant")},
        },
        "factions": out,
    }
    out_dir = OPTS.get("out") or HERE
    os.makedirs(out_dir, exist_ok=True)
    if OPTS.get("slim"):
        drop = ("stats_total", "avg_war_score_90d", "wars_60d", "wins_60d",
                "winrate_60d", "rank_level", "chain_tier", "torn_url")
        for r in out:
            for k in drop:
                r.pop(k, None)
    with open(os.path.join(out_dir, "factions.json"), "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    cols = ["id", "name", "tag", "respect", "members", "capacity", "open_slots", "rank_name",
            "best_chain", "age_days", "enlisted", "wars_total", "winrate_all", "winrate_90d",
            "wars_90d", "wars_per_month", "avg_margin_90d", "win_streak", "days_since_war",
            "respect_gain_30d", "pct_active_7d", "pct_inactive_30d", "level_median",
            "tenure_median", "stats_median", "stats_min", "stats_p10", "stats_top10_avg",
            "wall_1b", "wall_10b", "respect_invested_min", "torn_url"]
    with open(os.path.join(out_dir, "factions.csv"), "w", newline="") as f:
        wcsv = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        wcsv.writeheader()
        for r in out:
            wcsv.writerow(r)

    print("Wrote factions.json and factions.csv (%d factions)" % len(out))
    print("Coverage: %s" % json.dumps(payload["coverage"]))
    if excluded:
        print("Left out %d factions with no activity in %d days." % (excluded, dead_after))
    print("Open dashboard.html to explore.")


def _playstyle(r):
    """A plain-language label for what a faction actually does. Half the player
    base is not looking for a war machine, and a leaderboard that only measures
    warring quietly tells them all the wrong factions are good."""
    tags = []
    wpm = r.get("wars_per_month") or 0
    chain = r.get("best_chain") or 0
    terr = r.get("territories") or 0
    growth = r.get("respect_gain_per_day") or 0
    active = r.get("pct_active_7d")
    size = r.get("roster_size") or 0

    if wpm >= 1.0:
        tags.append("warring")
    elif wpm >= 0.3:
        tags.append("occasional war")
    if chain >= 10000:
        tags.append("chaining")
    if terr >= 3:
        tags.append("territory")
    if growth > 0 and wpm < 0.3 and chain < 10000:
        tags.append("crime & cash")
    if active is not None and active >= 0.8 and size >= 20:
        tags.append("very active")
    if not tags:
        tags.append("quiet")
    return tags


def _snapshot_near(snaps, target_ts):
    best, best_gap = None, None
    for s in snaps[:-1]:
        gap = abs(s["ts"] - target_ts)
        if best_gap is None or gap < best_gap:
            best, best_gap = s, gap
    return best


# Chain milestones and their cumulative respect cost in the core branch.
# Sourced from the public faction tree; used to put a floor under how much
# respect a faction has ploughed into upgrades, since the upgrade list itself
# is only visible to members.
CHAIN_TIERS = [
    (10, 0), (25, 250), (50, 750), (100, 2_000), (250, 5_500),
    (500, 15_000), (1000, 40_000), (2500, 110_000), (5000, 300_000),
    (10000, 800_000), (25000, 2_200_000), (50000, 6_000_000), (100000, 17_000_000),
]
CAPACITY_STEP_COST = 1_500  # fallback only; the real curve comes from torn/factiontree


def _chain_tier(best_chain):
    tier = 0
    for size, _ in CHAIN_TIERS:
        if (best_chain or 0) >= size:
            tier = size
    return tier


def _infer_investment(best_chain, capacity, tiers=None):
    total = 0
    for size, cost in (tiers or CHAIN_TIERS):
        if (best_chain or 0) >= size:
            total = cost
    steps = max(0, ((capacity or 0) - 10) // 5)
    total += steps * CAPACITY_STEP_COST
    return total


# --------------------------------------------------------------------------
# demo data
# --------------------------------------------------------------------------

def cmd_demo(conn, _cfg=None, _client=None):
    """Seed plausible synthetic data so the dashboard is explorable offline."""
    random.seed(7)
    now = int(time.time())
    words_a = ["Iron", "Black", "Crimson", "Silent", "Broken", "Golden", "Savage", "Pale",
               "Hollow", "Rust", "Cobalt", "Ashen", "Feral", "Grim", "Velvet", "Static"]
    words_b = ["Syndicate", "Order", "Cartel", "Company", "Union", "Collective", "Guard",
               "Division", "Chapter", "Front", "League", "Circle", "Works", "Assembly"]

    conn.executescript("DELETE FROM faction; DELETE FROM snapshot; DELETE FROM member;"
                       "DELETE FROM war; DELETE FROM war_side; DELETE FROM player_stat;"
                       "DELETE FROM territory;")

    tier_profiles = [(0.05, 3.0), (0.20, 1.2), (0.45, 0.35), (1.0, 0.08)]
    pid = 100000
    war_id = 5000

    for i in range(140):
        fid = 1000 + i * 7
        name = "%s %s" % (random.choice(words_a), random.choice(words_b))
        roll = random.random()
        strength = next(m for c, m in tier_profiles if roll <= c)
        conn.execute("INSERT INTO faction (id,name,tag,first_seen,source,last_enriched,dead)"
                     " VALUES (?,?,?,?,?,?,0)",
                     (fid, name, name[:3].upper(), now, "demo", now))

        cap = random.choice([25, 50, 75, 100, 100, 100])
        size = min(cap, int(cap * random.uniform(0.55, 1.0)))
        respect = int(random.lognormvariate(13, 1.6) * strength)
        chain = random.choice([100, 500, 1000, 5000, 10000, 25000, 50000])

        for weeks_ago in (8, 4, 0):
            ts = now - weeks_ago * 7 * DAY
            grown = int(respect * (1 - 0.012 * weeks_ago * strength))
            conn.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                fid, ts, grown, size, cap, chain,
                random.choice(["Bronze", "Silver", "Gold", "Platinum", "Diamond"]),
                random.randint(1, 3), random.randint(1, 400), random.randint(0, 60),
                random.randint(400, 4000), 0, 0, 1 if random.random() < 0.7 else 0))

        for _ in range(size):
            pid += 1
            act = now - int(random.expovariate(1 / (3.0 * DAY / strength)))
            conn.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?,?,?)", (
                fid, pid, now, "player%d" % pid,
                min(100, max(10, int(random.gauss(50 + 25 * math.log1p(strength), 18)))),
                int(random.expovariate(1 / 260)), act,
                random.choice(["Okay", "Okay", "Okay", "Hospital", "Traveling"]), "Member"))
            est = int(max(1e6, random.lognormvariate(20.5 + math.log1p(strength), 1.3)))
            conn.execute("INSERT INTO player_stat VALUES (?,?,?,?,?,?,?)",
                         (pid, now, est, int(est ** 0.5), 0, now, "bss"))

        n_wars = int(random.uniform(0, 14) * min(2.0, strength))
        for k in range(n_wars):
            war_id += 1
            end = now - random.randint(1, 200) * DAY
            won = random.random() < min(0.92, 0.30 + 0.22 * math.log1p(strength))
            my_score = random.randint(2000, 60000)
            opp = int(my_score * random.uniform(0.3, 0.95)) if won else int(my_score / random.uniform(0.3, 0.95))
            conn.execute("INSERT INTO war VALUES (?,?,?,?,?)",
                         (war_id, end - 2 * DAY, end, fid if won else -1, max(my_score, opp)))
            conn.execute("INSERT INTO war_side VALUES (?,?,?,?,?)",
                         (war_id, fid, name, my_score, chain))
            conn.execute("INSERT INTO war_side VALUES (?,?,?,?,?)",
                         (war_id, -1, "Opponent", opp, chain))
        if random.random() < 0.25:
            for t in range(random.randint(1, 6)):
                conn.execute("INSERT OR REPLACE INTO territory VALUES (?,?,?,?)",
                             ("%s-%d" % (name[:2].upper(), t), fid,
                              random.randint(100, 900), random.randint(10, 90)))
    conn.commit()
    for r in conn.execute("SELECT id FROM faction").fetchall():
        _classify(conn, r["id"], DEFAULT_CONFIG)
    conn.commit()
    print("Seeded 140 demo factions. Now run: python3 scout.py score")


# --------------------------------------------------------------------------
# misc commands
# --------------------------------------------------------------------------

def cmd_init(*_):
    if os.path.exists(CONFIG_PATH):
        print("config.json already exists, leaving it alone.")
        return
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    print("Wrote config.json. Add your Torn API key, then run: python3 scout.py run")


def cmd_status(conn, *_):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print("factions known:        %d" % q("SELECT COUNT(*) FROM faction"))
    print("  enriched:            %d" % q("SELECT COUNT(*) FROM faction WHERE last_enriched IS NOT NULL"))
    print("  gone/invalid:        %d" % q("SELECT COUNT(*) FROM faction WHERE dead=1"))
    print("snapshots:             %d" % q("SELECT COUNT(*) FROM snapshot"))
    print("  distinct dates:      %d" % q("SELECT COUNT(DISTINCT ts/86400) FROM snapshot"))
    print("member rows:           %d" % q("SELECT COUNT(*) FROM member"))
    print("stat estimates:        %d" % q("SELECT COUNT(*) FROM player_stat"))
    print("ranked wars:           %d" % q("SELECT COUNT(*) FROM war"))
    oldest = conn.execute("SELECT MIN(ts) FROM snapshot").fetchone()[0]
    if oldest:
        print("history depth:         %.1f days" % ((time.time() - oldest) / DAY))


HISTORY_PATH_DEFAULT = os.path.join(HERE, "history.json")
HISTORY_MAX_POINTS = 80


def _history_path():
    return OPTS.get("history") or HISTORY_PATH_DEFAULT


def cmd_history_save(conn, *_):
    """Growth tracking is the one thing that needs continuity across runs, and it
    only needs four numbers per faction per run. Keeping that in a small JSON file
    means the database itself can be thrown away and rebuilt anywhere."""
    series = defaultdict(list)
    for r in conn.execute(
            "SELECT s.faction_id, s.ts, s.respect, s.member_count, s.capacity "
            "FROM snapshot s JOIN faction f ON f.id = s.faction_id "
            "WHERE f.dead = 0 ORDER BY s.ts"):
        series[str(r["faction_id"])].append(
            [r["ts"], r["respect"], r["member_count"], r["capacity"]])

    # Thin to one point per day, newest kept, then cap the length.
    out = {}
    for fid, pts in series.items():
        seen, keep = set(), []
        for p in reversed(pts):
            day = p[0] // DAY
            if day in seen:
                continue
            seen.add(day)
            keep.append(p)
            if len(keep) >= HISTORY_MAX_POINTS:
                break
        out[fid] = list(reversed(keep))

    path = _history_path()
    with open(path, "w") as f:
        json.dump({"version": 1, "saved": int(time.time()), "series": out},
                  f, separators=(",", ":"))
    pts = sum(len(v) for v in out.values())
    print("Saved history: %d factions, %d points, %.1f KB"
          % (len(out), pts, os.path.getsize(path) / 1024))


def cmd_history_load(conn, *_):
    path = _history_path()
    if not os.path.exists(path):
        print("No history file at %s — starting fresh." % path)
        return
    with open(path) as f:
        blob = json.load(f)
    n = 0
    for fid, pts in (blob.get("series") or {}).items():
        fid = int(fid)
        note_faction(conn, fid, source="history")
        for p in pts:
            ts, respect = int(p[0]), int(p[1])
            members = int(p[2]) if len(p) > 2 else 0
            cap = int(p[3]) if len(p) > 3 else 0
            exists = conn.execute("SELECT 1 FROM snapshot WHERE faction_id=? AND ts=?",
                                  (fid, ts)).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO snapshot (faction_id,ts,respect,member_count,capacity) "
                "VALUES (?,?,?,?,?)", (fid, ts, respect, members, cap))
            n += 1
    conn.commit()
    print("Loaded history: %d snapshot rows across %d factions"
          % (n, len(blob.get("series") or {})))


def cmd_prune(conn, *_):
    """Rosters are the bulk of the database and only the newest one is used —
    tenure and churn come from days_in_faction, not from stored history."""
    before = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    conn.execute("""
        DELETE FROM member WHERE (faction_id, ts) NOT IN
        (SELECT faction_id, MAX(ts) FROM member GROUP BY faction_id)
    """)
    conn.execute("DELETE FROM player_stat WHERE ts < ?", (int(time.time()) - 45 * DAY,))
    # Thin snapshots to one per faction per day.
    conn.execute("""
        DELETE FROM snapshot WHERE rowid NOT IN
        (SELECT MAX(rowid) FROM snapshot GROUP BY faction_id, ts/86400)
    """)
    conn.commit()
    conn.execute("VACUUM")
    after = os.path.getsize(DB_PATH)
    print("Pruned database: %.1f MB -> %.1f MB" % (before / 1e6, after / 1e6))


def cmd_doctor(conn, cfg, client):
    """Tests every endpoint this tool depends on and reports what actually came
    back. Run this before blaming a long job for being stuck."""
    print("=" * 62)
    print("Checking each endpoint. Any line marked FAIL explains a stalled run.")
    print("=" * 62)
    results = []

    def check(label, fn):
        t = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "exception: %s" % e
        ms = (time.time() - t) * 1000
        results.append(ok)
        print("%-4s %-34s %6.0fms  %s" % ("ok" if ok else "FAIL", label, ms, detail))

    def key_info():
        d, err = client.call("key/info", version=2)
        if err:
            d2, err2 = client.call("user/", version=1, selections="basic")
            if err2:
                return False, err2.get("error", "?")
            return True, "key works (v1). Player: %s" % d2.get("name")
        acc = (d.get("info") or {}).get("access") or {}
        return True, "access level: %s" % (acc.get("type") or acc.get("level") or "unknown")

    def hof():
        d, err = client.call("torn/factionhof", version=2, cat="respect", limit=10, offset=0)
        if err:
            return False, "%s — discovery will find far fewer factions" % err.get("error")
        rows = _unwrap_hof(d)
        if not rows:
            return False, "returned no rows (shape may have changed)"
        return True, "%d factions, top is %s" % (len(rows), rows[0].get("name", "?"))

    def rankedwars():
        now = int(time.time())
        d, err = client.call("torn/", version=1, selections="rankedwars",
                             **{"from": now - 30 * 86400, "to": now})
        if err:
            return False, "%s — no win/loss data at all" % err.get("error")
        w = (d or {}).get("rankedwars") or {}
        return bool(w), "%d wars in the last 30 days" % len(w)

    def faction_v1():
        d, err = client.call("faction/8", version=1, selections="basic")
        if err:
            return False, "%s — every faction will cost 3 calls instead of 1" % err.get("error")
        mem = d.get("members") or {}
        if not mem:
            return False, "basic works but returned no roster — enrich falls back to v2, 2x slower"
        return True, "%s, %d members in one call" % (d.get("name"), len(mem))

    def faction_v2():
        d, err = client.call("faction/8/basic", version=2)
        if err:
            return False, err.get("error")
        b = d.get("basic") or d
        return bool(b.get("name")), "%s" % b.get("name", "?")

    def ffscouter():
        k = (cfg.get("ffscouter_key") or "").strip()
        if not k:
            return False, "no ffscouter_key set — stat columns stay empty (optional)"
        url = ("https://ffscouter.com/api/v1/check-key?"
               + urllib.parse.urlencode({"key": k}))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        if d.get("is_registered"):
            return True, "key registered"
        return False, "key not registered at ffscouter.com — sign up there first"

    check("Torn API key", key_info)
    check("torn/factionhof (discovery)", hof)
    check("torn/rankedwars (war records)", rankedwars)
    check("faction/{id} v1 basic (rosters)", faction_v1)
    check("faction/{id}/basic v2 (fallback)", faction_v2)
    check("FFScouter (stat estimates)", ffscouter)

    print("-" * 62)
    known = conn.execute("SELECT COUNT(*) c FROM faction WHERE dead=0").fetchone()["c"]
    todo = conn.execute(
        "SELECT COUNT(*) c FROM faction WHERE dead=0 AND last_enriched IS NULL").fetchone()["c"]
    rate = cfg.get("requests_per_minute", 55)
    print("Factions known: %d, still to enrich: %d" % (known, todo))
    if todo:
        print("At %d calls/min that step alone needs about %s."
              % (rate, _dur(todo * 60.0 / rate)))
    print("A run that prints nothing for minutes is usually buffering, not hanging.")
    print("If every line above says ok, let it run.")


def cmd_run(conn, cfg, client):
    cmd_history_load(conn)
    cmd_factiontree(conn, cfg, client)
    cmd_discover(conn, cfg, client)
    cmd_territory(conn, cfg, client)
    cmd_enrich(conn, cfg, client)
    cmd_stats(conn, cfg, client)
    cmd_prune(conn)
    cmd_score(conn, cfg, client)
    cmd_history_save(conn)


def main():
    global CFG
    # CI captures stdout through a pipe, which makes Python block-buffer it and
    # makes a working job look frozen. Force line buffering.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    p = argparse.ArgumentParser(description="Rank Torn factions from public API data.")
    p.add_argument("command", choices=["init", "demo", "discover", "wars", "enrich",
                                       "stats", "score", "run", "snapshot", "status",
                                       "prune", "history-save", "history-load", "doctor",
                                       "sweep", "territory", "factiontree"])
    p.add_argument("--out", metavar="DIR",
                   help="where to write factions.json and factions.csv")
    p.add_argument("--slim", action="store_true",
                   help="drop rarely-read fields to shrink the payload for the web")
    p.add_argument("--history", metavar="FILE", help="path to history.json")
    args = p.parse_args()
    OPTS.update({"out": args.out, "slim": args.slim, "history": args.history})

    if args.command == "init":
        return cmd_init()

    conn = open_db()
    needs_key = args.command in ("discover", "wars", "enrich", "stats", "run",
                                "snapshot", "doctor", "sweep", "territory", "factiontree")
    cfg = load_config() if os.path.exists(CONFIG_PATH) else DEFAULT_CONFIG
    CFG.update(cfg)
    client = None
    if needs_key:
        key = (cfg.get("torn_api_key") or os.environ.get("TORN_API_KEY") or "").strip()
        if not key:
            sys.exit("Set torn_api_key in config.json (or the TORN_API_KEY env var).")
        client = TornClient(key, RateLimiter(cfg.get("requests_per_minute", 55)))

    fn = {"demo": cmd_demo, "discover": cmd_discover, "wars": cmd_wars,
          "enrich": cmd_enrich, "stats": cmd_stats, "score": cmd_score,
          "run": cmd_run, "snapshot": cmd_snapshot, "status": cmd_status,
          "prune": cmd_prune, "history-save": cmd_history_save,
          "history-load": cmd_history_load, "doctor": cmd_doctor,
          "sweep": cmd_sweep, "territory": cmd_territory,
          "factiontree": cmd_factiontree}[args.command]

    start = time.time()
    try:
        fn(conn, cfg, client)
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved — rerun the same command to continue.")
        conn.commit()
        conn.close()
        sys.exit(130)
    except BrokenPipeError:
        os._exit(0)   # output was piped into something that closed early
    except Exception:
        import traceback
        conn.commit()   # never throw away work already done
        print("\n" + "=" * 62)
        print("The '%s' command failed. Everything up to this point was saved." % args.command)
        print("=" * 62)
        traceback.print_exc()
        print("=" * 62)
        print("Rerun just this step to continue: python3 scout.py %s" % args.command)
        conn.close()
        sys.exit(1)
    if client:
        print("(%d API calls, %.1f min)" % (client.calls, (time.time() - start) / 60))
    conn.close()


if __name__ == "__main__":
    main()
