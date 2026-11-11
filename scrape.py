#!/usr/bin/env python3
"""
scrape.py — collect public tweets about data centers in Argentina and store them
in Supabase.

    python scrape.py --mode daily                 # cron: new tweets since the last run
    python scrape.py --mode backfill              # one-off deep sweep of history
    python scrape.py --mode test                  # smoke test, ≤100 tweets fetched (≈ $0.015)
    python scrape.py --mode daily --dry-run --csv out/x.csv   # fetch, don't write, dump the raw CSV

The scraper collects. The Colab notebook filters.

Cost-saving gates (this file):
    * the search query (CORE_TERMS + Argentina anchor, -filter:nativeretweets)
    * skip tweet ids already in dc_tweets_raw
    * stop a Latest query once a page is entirely ids we already have
    * daily mode does not re-walk historical weeks (see config.DAILY)
    * expand a thread only when the seed is new or its reply_count grew
    * is_on_topic picks which outlet posts are worth expanding — it does not drop tweets
    * budget cap (BUDGET_USD) and per-run hard cap

Analysis filters (Colab, via filters.content_reason — NOT applied here):
    bot, short text, off-topic, duplicate text.

Every fetched tweet is written to dc_tweets_raw. Retweets stay there with
drop_reason "retweet". Everything else is also written to dc_tweets, which the
Streamlit app reads. source_type is a label (filters.classify), not a drop.

Rows are flushed to Supabase after each query, and again if the run dies, so a
dropped connection does not throw away calls we already paid for.

Env vars:  TWITTERAPI_IO_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse                             # command-line flags
import csv                                  # CSV output
import json                                 # dc_runs.notes is a JSON string
import os                                   # environment variables (API keys)
import sys                                  # to add the repo folder to the import path
import time                                 # sleep between retries
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests                             # HTTP calls to the API

sys.path.insert(0, str(Path(__file__).parent))      # so `import config` works from any folder
import config as C  # noqa: E402
from filters import (parse_dt, author_is_verified, is_on_topic,  # noqa: E402
                     tweet_topic_text, collection_reason, classify, to_row)

API = "https://api.twitterapi.io/twitter/tweet/advanced_search"   # the search endpoint
COST_PER_TWEET = 0.00015  # $0.15 per 1,000 tweets returned
MIN_CALL_COST = 0.00015   # each call costs at least 15 credits, even if it returns nothing
# twitterapi.io sometimes closes the socket. These are worth retrying.
RETRYABLE = (
    requests.exceptions.ConnectionError,          # "Remote end closed connection without response"
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


# ---------------------------------------------------------------------------
# Pure helpers (no network, no database) — unit-tested
# ---------------------------------------------------------------------------
def fresh_since(now: datetime, lookback_hours: float, overlap_hours: float,
                max_catchup_hours: float, watermark: datetime | None) -> datetime:
    """Start of the fresh search window.

    No watermark yet (first run, or a run from before this cursor existed):
    now minus lookback_hours (26 h on the daily cron).

    Watermark = the end of the last run that finished every query. The next
    window starts overlap_hours before it, so a tweet posted while the previous
    run was in flight is still fetched. A same-day rerun therefore asks for
    ~2 h, not another 26 h. A missed run leaves an old watermark, so the window
    widens to cover the gap, but never further back than max_catchup_hours
    (use --mode backfill past that).
    """
    default = now - timedelta(hours=lookback_hours)
    if watermark is None:
        return default
    start = watermark - timedelta(hours=overlap_hours)
    earliest = now - timedelta(hours=max_catchup_hours)
    if start < earliest:
        start = earliest
    if start >= now:                                # clock skew: watermark in the future
        return default
    return start


def claim_watermark(now: datetime, previous: datetime | None, *,
                    aborted: bool, over_cap: bool, redo: list) -> datetime | None:
    """Watermark to store for the next run.

    A finished run claims `now`: everything up to this moment was requested.
    A run that died, hit the hard cap, or left queries unfinished keeps the
    previous watermark, so the next run still covers the hole.
    """
    if aborted or over_cap or redo:
        return previous
    return now


def page_all_known(tweets: list[dict], already: set[str]) -> bool:
    """True when every tweet on this page is an id we already stored.

    Latest order is newest-first, so the next page is older. If this page is
    entirely known, older pages are known too and we should not pay for them.
    A tweet with no id does not count as known (we refuse to stop on a guess).
    `already` must be the ids known BEFORE this page was processed — the caller
    checks that, then adds the page.
    """
    if not tweets:
        return False
    for t in tweets:
        tid = str(t.get("id") or "")
        if not tid or tid not in already:
            return False
    return True


def is_thread_seed(t: dict, outlets: set[str]) -> bool:
    """True if this post is worth paying to expand into a thread.

    Cost gate, not an analysis filter: an outlet (or a large verified account)
    whose own non-reply post is on-topic and drew replies. An off-topic viral
    thread is not expanded. The tweet itself is still stored either way.
    """
    a = t.get("author") or {}
    un = (a.get("userName") or "").lower()
    big = author_is_verified(a) and (a.get("followers") or 0) >= C.AUTO_OUTLET_MIN_FOLLOWERS
    if un not in outlets and not big:
        return False
    if (t.get("replyCount") or 0) < C.AUTO_OUTLET_MIN_REPLIES:
        return False
    if t.get("isReply"):
        return False
    return is_on_topic(tweet_topic_text(t))


def thread_needs_expansion(t: dict, known: set[str], reply_counts: dict[str, int]) -> bool:
    """True if we should spend API calls on this thread.

    A new seed (id not stored yet) is expanded. A seed we already have is
    expanded only when X's replyCount is higher than the reply_count we stored
    last time — otherwise the replies have not changed and the pages would be
    ids we already paid for.
    """
    tid = str(t.get("id") or "")
    if not tid or tid not in known:
        return True                                 # new post, or no id (let the caller skip)
    live = int(t.get("replyCount") or 0)
    prev = reply_counts.get(tid)
    if prev is None:
        return True                                 # stored, but we never recorded a count
    return live > int(prev)


def pages_for_fresh(mode: str, since: int, until: int, digging: bool) -> int:
    """How many pages to allow for one fresh query.

    A normal 26 h window uses fresh_pages_per_query (2). A catch-up window, or
    a query the previous run did not finish (`digging`), may use the larger
    per-week cap. Early-stop on a known page still applies unless digging.
    """
    P = C.MODES[mode]
    wide = P.get("max_pages_per_query_window", P.get("fresh_pages_per_query", 2))
    if digging:
        return wide
    hours = (until - since) / 3600
    lookback = P.get("lookback_hours", hours)
    if hours > lookback + 1:
        return wide
    return P.get("fresh_pages_per_query", wide)


# ---------------------------------------------------------------------------
# HTTP client: talks to the API, follows pages, counts what we fetched
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, key: str, hard_cap: int, session=None, sleep=time.sleep):
        self.s = session or requests.Session()      # reuses the connection between calls
        self.s.headers["X-API-Key"] = key           # authentication header on every request
        self._sleep = sleep                         # injectable so tests don't really wait
        self.pages = 0                              # API calls made
        self.tweets_fetched = 0                     # tweets returned so far (what we pay for)
        self.hard_cap = hard_cap                    # stop fetching once we reach this many
        self.exhausted = False                      # set when the API says no credits left (402)
        self.aborted = False                        # set when the connection stays down after retries
        self.query_failed = False                   # last search() hit an HTTP error that retries didn't clear
        self.capped = False                         # last search() skipped a further page because the hard cap was hit
        self.last_page_stop = False                 # last search() ended because a page was all known

    def over_cap(self) -> bool:
        """True when we must stop: out of credits, or at the per-run cap."""
        return self.exhausted or self.tweets_fetched >= self.hard_cap

    def search(self, query: str, query_type: str = "Latest", max_pages: int = 3,
               stop_if_page=None):
        """Yield tweet dicts for one query, following the cursor up to max_pages pages.

        stop_if_page(tweets) is checked BEFORE the page is yielded, against whatever
        ids the caller already has. If it returns True the page is still yielded
        (we already paid for it) but the next page is not requested.
        """
        self.last_page_stop = False
        self.query_failed = False
        self.capped = False
        cursor = ""                                 # "" = first page
        for _ in range(max_pages):
            if self.aborted:
                return
            if self.over_cap():
                self.capped = True                  # there may be older pages we did not pay for
                return
            r = None
            for attempt in range(4):                # retry up to 4 times
                try:
                    r = self.s.get(API, params={"query": query, "queryType": query_type,
                                                "cursor": cursor}, timeout=60)
                except RETRYABLE as e:
                    wait = min(60, 2 ** (attempt + 1))       # 2, 4, 8, 16 s
                    print(f"  ! {type(e).__name__} from twitterapi.io, "
                          f"retry {attempt + 1}/4 in {wait}s")
                    self._sleep(wait)
                    r = None
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    wait = min(60, 2 ** (attempt + 1))
                    print(f"  ! HTTP {r.status_code}, retry {attempt + 1}/4 in {wait}s")
                    self._sleep(wait)
                    continue
                break
            if r is None:
                # The socket died on every try. Keep what we already stored and stop
                # the run — the next query would fail the same way.
                print("  ! twitterapi.io connection failed after retries; "
                      "saving tweets already fetched")
                self.aborted = True
                return
            if r.status_code == 402 or (r.status_code in (401, 403) and "credit" in r.text.lower()):
                print(f"  ! {r.status_code} — account out of credits, stopping all requests")
                self.exhausted = True
                return
            if r.status_code != 200:                # any other error: give up on this query only
                print(f"  ! {r.status_code} on {query[:60]}…: {r.text[:200]}")
                self.query_failed = True            # caller must not mark the query finished
                return
            d = r.json()                            # the response body
            self.pages += 1
            tweets = d.get("tweets") or []          # this page's tweets (about 20)
            self.tweets_fetched += len(tweets)
            # Decide before the caller adds these ids to `seen`, or a page of brand-new
            # tweets would look "already known" and we would stop too early.
            stop = bool(tweets) and stop_if_page is not None and stop_if_page(tweets)
            for t in tweets:
                yield t                             # hand each tweet to the caller
            if stop:
                self.last_page_stop = True
                return
            if not d.get("has_next_page") or not tweets:
                return                              # no more pages
            cursor = d.get("next_cursor") or ""     # token for the next page
            if not cursor:
                return

    def est_cost(self) -> float:
        """Estimated dollars spent this run."""
        return max(self.tweets_fetched * COST_PER_TWEET, self.pages * MIN_CALL_COST)


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def get_supabase():
    from supabase import create_client              # imported here so --dry-run needs no supabase package
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def spent_so_far(sb) -> float:
    """Sum of the estimated cost of every previous run (the budget guard reads this)."""
    try:
        res = sb.table(C.SUPABASE_RUNS_TABLE).select("est_cost_usd").execute()
        return float(sum(float(r["est_cost_usd"] or 0) for r in (res.data or [])))
    except Exception:
        return 0.0                                  # table missing → assume nothing spent


def _page_ids(sb, table: str) -> tuple[set[str], dict[str, int]]:
    """Every tweet_id in `table`, plus the reply_count stored for each (thread watermark)."""
    ids, replies, start, step = set(), {}, 0, 1000
    while True:
        res = sb.table(table).select("tweet_id,reply_count").range(start, start + step - 1).execute()
        data = res.data or []
        for r in data:
            tid = str(r["tweet_id"])
            ids.add(tid)
            if r.get("reply_count") is not None:
                replies[tid] = int(r["reply_count"])
        if len(data) < step:                        # short page = last page
            return ids, replies
        start += step


def load_known(sb) -> tuple[set[str], dict[str, int]]:
    """Ids we have already paid for, and the reply_count last stored for each.

    Read from dc_tweets_raw, not dc_tweets. A tweet the old filters dropped is
    still in the raw table; fetching it again would charge us twice. Falls
    back to dc_tweets only if the raw table cannot be read.
    """
    if sb is None:
        return set(), {}
    try:
        return _page_ids(sb, C.SUPABASE_RAW_TABLE)
    except Exception as e:
        print(f"  ! could not read {C.SUPABASE_RAW_TABLE} ({str(e)[:80]})")
    try:
        print(f"  ! known ids taken from {C.SUPABASE_TABLE} instead")
        return _page_ids(sb, C.SUPABASE_TABLE)
    except Exception as e:
        print(f"  ! could not read {C.SUPABASE_TABLE} ({str(e)[:80]})")
        return set(), {}


def load_latest_notes(sb) -> dict:
    """notes JSON of the most recent dc_runs row, or {} if there is none."""
    if sb is None:
        return {}
    try:
        res = (sb.table(C.SUPABASE_RUNS_TABLE).select("notes")
               .order("id", desc=True).limit(1).execute())
    except Exception as e:
        print(f"  ! could not read {C.SUPABASE_RUNS_TABLE} ({str(e)[:80]})")
        return {}
    rows = res.data or []
    if not rows or not rows[0].get("notes"):
        return {}
    notes = rows[0]["notes"]
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except json.JSONDecodeError:
            return {}
    return notes if isinstance(notes, dict) else {}


def upsert(sb, rows: list[dict], table: str, *, ignore_duplicates: bool = True) -> int:
    """Insert rows in batches of 200.

    ignore_duplicates=True leaves an existing tweet_id untouched (safe to flush
    twice). False updates the stored row — used only to fix source_type labels
    on tweets this same run just inserted.
    """
    n = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        sb.table(table).upsert(chunk, on_conflict="tweet_id",
                               ignore_duplicates=ignore_duplicates).execute()
        n += len(chunk)
    return n


def load_coverage(sb) -> set[tuple]:
    """The ledger of (window_start, window_end, query_idx, query_type) cells already swept.
    Lets a later backfill skip weeks it has already paid for."""
    if sb is None:
        return set()
    try:
        res = sb.table(C.SUPABASE_COVERAGE_TABLE).select(
            "window_start,window_end,query_idx,query_type").execute()
    except Exception as e:  # table missing → behave as if nothing were covered
        print(f"  ! coverage table unavailable ({str(e)[:80]}); every window will be fetched")
        return set()
    out = set()
    for r in res.data or []:
        ws = parse_dt(r["window_start"]); we = parse_dt(r["window_end"])
        if ws is None or we is None:
            continue
        out.add((int(ws.timestamp()), int(we.timestamp()), int(r["query_idx"]), r["query_type"]))
    return out


def is_covered(covered: set[tuple], since: int, until: int, qi: int, qt: str) -> bool:
    """True if this exact cell was swept, or a larger window containing it was."""
    if (since, until, qi, qt) in covered:
        return True
    return any(ws <= since and we >= until and cqi == qi and cqt == qt
               for ws, we, cqi, cqt in covered)


def mark_coverage(sb, since: int, until: int, qi: int, qt: str, fetched: int):
    """Record a swept cell in the ledger. Call this only after that cell's tweets are flushed."""
    if sb is None:
        return
    try:
        sb.table(C.SUPABASE_COVERAGE_TABLE).upsert({
            "window_start": datetime.fromtimestamp(since, timezone.utc).isoformat(),
            "window_end": datetime.fromtimestamp(until, timezone.utc).isoformat(),
            "query_idx": qi, "query_type": qt, "fetched": fetched,
        }).execute()
    except Exception as e:
        print(f"  ! could not record coverage: {str(e)[:80]}")


# ---------------------------------------------------------------------------
# Queries and time windows
# ---------------------------------------------------------------------------
def build_queries() -> list[str]:
    """Fill the templates in config.SEARCH_QUERIES. The outlet template is repeated
    once per chunk of TO_OUTLETS_PER_QUERY handles so no query exceeds X's length cap."""
    out = []
    n = C.TO_OUTLETS_PER_QUERY
    chunks = [C.OUTLETS[i:i + n] for i in range(0, len(C.OUTLETS), n)]   # [[12 handles], [12 handles], …]
    for q in C.SEARCH_QUERIES:
        if "{to_outlets}" in q:                     # the "replies to outlets" template
            for ch in chunks:
                out.append(q.format(core=C.CORE_TERMS, ar=C.ARGENTINA_ANCHORS, co=C.COMPANY_ANCHORS,
                                    to_outlets=" OR ".join(f"to:{h}" for h in ch)))
        else:                                       # the keyword templates
            out.append(q.format(core=C.CORE_TERMS, ar=C.ARGENTINA_ANCHORS, co=C.COMPANY_ANCHORS,
                                to_outlets=""))
    for q in out:
        assert len(q) + 45 <= 512, f"query too long ({len(q)}): {q[:80]}…"   # 45 chars left for since/until
    return out


def time_windows(mode: str, now: datetime | None = None,
                 watermark: datetime | None = None) -> tuple[tuple[int, int] | None, list[tuple[int, int]]]:
    """Return (fresh, hist):
    fresh = (start, end) Unix timestamps for the fresh phase, or None
    hist  = ISO-week windows, newest first.

    Daily (backfill_history False) returns only the current partial week, so a
    tweet older than the fresh window but still inside this week can be caught,
    and completed weeks are not requested again. Backfill / bonus walk every
    week back to HISTORY_START. Weeks start on Monday 00:00 UTC so the same
    cells recur and the coverage ledger can skip them.
    """
    P = C.MODES[mode]
    now = now or datetime.now(timezone.utc)
    fresh = None
    if "lookback_hours" in P:                       # only the daily/test modes have a fresh phase
        start = fresh_since(now, P["lookback_hours"], P.get("fresh_overlap_hours", 2),
                            P.get("max_catchup_hours", P["lookback_hours"]), watermark)
        fresh = (int(start.timestamp()), int(now.timestamp()))
    hist = []
    if P.get("start_date"):
        floor = datetime.fromisoformat(P["start_date"]).replace(tzinfo=timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        hist.append((int(monday.timestamp()), int(now.timestamp())))     # the current, partial week
        # Default: walk history when the mode has a start_date. Daily sets the flag False.
        walk = P["backfill_history"] if "backfill_history" in P else True
        if walk:
            end = monday
            while end > floor:                      # walk back one week at a time
                start_dt = end - timedelta(days=C.WINDOW_DAYS)
                hist.append((int(max(start_dt, floor).timestamp()), int(end.timestamp())))
                end = start_dt
    return fresh, hist


def _as_int_list(value) -> list[int]:
    """notes.redo_* is a list of query indexes. Ignore anything that isn't."""
    if not isinstance(value, list):
        return []
    out = []
    for x in value:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run(mode: str, dry_run: bool, out_csv: str | None) -> int:
    P = C.MODES[mode]                               # the caps for this mode (daily / test / …)
    client = Client(os.environ["TWITTERAPI_IO_KEY"], P["hard_cap_tweets_fetched"])
    outlets = {h.lower() for h in C.OUTLETS}        # lowercase handles; grows with auto-discovered outlets
    now = datetime.now(timezone.utc)

    # ---- Budget guard, known ids, cursor from the previous run -------------
    sb = None
    run_id = None
    known: set[str] = set()                         # tweet_ids already fetched (raw table)
    reply_counts: dict[str, int] = {}               # tweet_id → reply_count last stored
    prev_notes: dict = {}
    if not dry_run:
        sb = get_supabase()
        budget = float(os.environ.get("BUDGET_USD", C.BUDGET_USD))
        spent = spent_so_far(sb)
        print(f"spent so far ≈ ${spent:.2f} of ${budget:.2f} budget")
        if spent >= budget:
            print("budget reached — not running. Raise BUDGET_USD (env or config) after recharging.")
            return 0
        remaining_tweets = int((budget - spent) / COST_PER_TWEET)   # how many more tweets the budget allows
        client.hard_cap = min(client.hard_cap, remaining_tweets)    # never overshoot it
        # Read the cursor BEFORE inserting this run, or we would read our own empty row.
        prev_notes = load_latest_notes(sb)
        known, reply_counts = load_known(sb)
        print(f"{len(known)} tweets already stored")
        # Seed the row with the previous cursor. If we die before the first flush, the
        # next run still sees that cursor (and partial=True) instead of an empty notes field.
        seed_notes = {
            "run_kind": "fresh" if "lookback_hours" in P else "history",
            "partial": True,
            "fresh_watermark": prev_notes.get("fresh_watermark"),
            "redo_fresh": prev_notes.get("redo_fresh") or [],
            "redo_week": prev_notes.get("redo_week") or [],
            "analysis_filters": "colab",
        }
        run_row = sb.table(C.SUPABASE_RUNS_TABLE).insert(
            {"run_mode": mode, "notes": json.dumps(seed_notes)}).execute()
        run_id = run_row.data[0]["id"]

    prev_wm = parse_dt(prev_notes.get("fresh_watermark")) if prev_notes.get("fresh_watermark") else None
    prev_redo_fresh = set(_as_int_list(prev_notes.get("redo_fresh")))
    prev_redo_week = set(_as_int_list(prev_notes.get("redo_week")))
    # A daily run that died before it could record which queries finished: dig once,
    # don't trust early-stop (the newest page can be known while an older page was never stored).
    dig_all = (prev_notes.get("run_kind") == "fresh" and prev_notes.get("partial")
               and not prev_redo_fresh and not prev_redo_week)

    # ---- State for this run -------------------------------------------------
    seen: dict[str, dict] = {}        # tweet_id → tweet, new non-retweets (counts toward the target)
    seen_query: dict[str, str] = {}   # tweet_id → the query that found it
    seen_thread: dict[str, str] = {}  # tweet_id → outlet handle, for tweets found by thread expansion
    rejected_ids: set[str] = set()    # retweet ids seen this run (so the next page can early-stop)
    collected: list[dict] = []        # every new tweet this run, in memory, so we can relabel and write CSV
    pending: list[dict] = []          # subset not flushed yet
    reply_updates: list[dict] = []    # seeds whose reply_count grew; pushed with the next flush
    yields: list[dict] = []           # per-query unique yield, for the log and dc_runs.notes
    dropped = {"retweet": 0, "known": 0, "over_target": 0}
    baseline = set(known)             # ids that were stored before this run (yield stats don't move this)
    done = {"fresh": set(), "week": set()}          # query indexes whose Latest pass finished
    threads_expanded = 0
    threads_skipped = 0
    target = P["target_new_tweets"]                 # new stored rows wanted before we stop fetching

    def already(tid: str) -> bool:
        return tid in known or tid in seen or tid in rejected_ids

    def consider(t: dict, query: str, thread_of: str | None = None) -> str | None:
        """Record one fetched tweet. Returns the drop_reason, or None if it is a normal keep.

        Known ids are not stored again. Retweets are stored in the raw table only.
        Everything else is stored in both tables. Past `target`, the row is still
        stored (the API call is already spent) and labelled over_target.
        """
        tid = str(t.get("id") or "")
        if not tid or tid in seen or tid in rejected_ids:
            return "known"
        if tid in known:
            dropped["known"] += 1
            return "known"
        reason = collection_reason(t, len(seen), target)   # retweet / over_target / None
        item = {"t": t, "query": query, "thread_of": thread_of, "reason": reason}
        if reason == "retweet":
            dropped["retweet"] += 1
            rejected_ids.add(tid)
        else:
            if reason == "over_target":
                dropped["over_target"] += 1
            if thread_of:
                seen_thread[tid] = thread_of
            seen[tid] = t
            seen_query[tid] = query
        collected.append(item)
        pending.append(item)
        return reason

    def build_raw(item: dict) -> dict:
        """Flatten one collected tweet into a dc_tweets_raw row, labelling source_type now."""
        t = item["t"]
        st, oh = classify(t, outlets, thread_of=item.get("thread_of"))
        item["source_type"] = st
        item["outlet_handle"] = oh
        row = to_row(t, st, oh, item["query"], mode)
        return {**row, "drop_reason": item["reason"],
                "found_via": "thread" if item.get("thread_of") else "search",
                "thread_of": item.get("thread_of"), "run_id": run_id, "legacy": False}

    def build_clean(item: dict) -> dict:
        """The same row for dc_tweets (no drop_reason column on that table)."""
        t = item["t"]
        return to_row(t, item["source_type"], item["outlet_handle"], item["query"], mode)

    def notes_payload(partial: bool, watermark: datetime | None,
                      redo_fresh: list[int], redo_week: list[int]) -> dict:
        counts: dict[str, int] = {}
        for item in collected:
            if item["reason"] == "retweet":
                continue
            st = item.get("source_type") or "?"
            counts[st] = counts.get(st, 0) + 1
        return {
            "run_kind": "fresh" if "lookback_hours" in P else "history",
            "partial": partial,
            "fresh_watermark": watermark.isoformat() if watermark else None,
            "redo_fresh": redo_fresh,
            "redo_week": redo_week,
            "by_source": counts,
            "dropped": dropped,
            "analysis_filters": "colab",       # bot / short / offtopic / dup_text are not applied here
            "threads_expanded": threads_expanded,
            "threads_skipped": threads_skipped,
            "query_yield": yields,
            "auto_outlets": sorted(outlets - {h.lower() for h in C.OUTLETS}),
        }

    def redo_lists() -> tuple[list[int], list[int]]:
        """Query indexes that did not finish. Empty means the fresh/week phases completed.

        A history-only mode (backfill / bonus) must not clear the daily cursor:
        it keeps whatever redo lists the previous run recorded.
        """
        if "lookback_hours" not in P:
            return sorted(prev_redo_fresh), sorted(prev_redo_week)
        n = len(queries)
        fresh_redo = [i for i in range(n) if i not in done["fresh"]] if fresh else []
        week_redo = [i for i in range(n) if i not in done["week"]] if hist else []
        return fresh_redo, week_redo

    def touch_run(partial: bool):
        """Write the running totals. A crash after this keeps the cost and the tweets."""
        if sb is None or run_id is None:
            return
        fresh_redo, week_redo = redo_lists()
        # While the run is still going, don't claim "now" — the next run must cover the rest.
        wm = prev_wm if partial else claim_watermark(
            now, prev_wm, aborted=client.aborted, over_cap=client.over_cap(),
            redo=fresh_redo + week_redo)
        try:
            sb.table(C.SUPABASE_RUNS_TABLE).update({
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "fetched": client.tweets_fetched,
                "inserted": len(seen),
                "api_pages": client.pages,
                "est_cost_usd": round(client.est_cost(), 4),
                "notes": json.dumps(notes_payload(partial, wm, fresh_redo, week_redo)),
            }).eq("id", run_id).execute()
        except Exception as e:
            print(f"  ! could not update dc_runs: {str(e)[:80]}")

    def push_reply_counts():
        """Store the reply_count we just expanded, so tomorrow's run can skip a quiet thread."""
        if sb is None or not reply_updates:
            return
        by_id = {}
        for u in reply_updates:
            by_id[u["tweet_id"]] = u["reply_count"]
        reply_updates.clear()
        for tid, n in by_id.items():
            reply_counts[tid] = n
            try:
                sb.table(C.SUPABASE_RAW_TABLE).update({"reply_count": n}).eq("tweet_id", tid).execute()
                sb.table(C.SUPABASE_TABLE).update({"reply_count": n}).eq("tweet_id", tid).execute()
            except Exception as e:
                print(f"  ! could not update reply_count for {tid}: {str(e)[:80]}")

    def flush():
        """Write tweets collected since the last flush. Safe to call twice."""
        if pending:
            raw_rows = [build_raw(item) for item in pending]
            clean_rows = [build_clean(item) for item in pending if item["reason"] != "retweet"]
            if sb is not None:
                upsert(sb, raw_rows, C.SUPABASE_RAW_TABLE)
                if clean_rows:
                    upsert(sb, clean_rows, C.SUPABASE_TABLE)
                # Treat them as known so a later query in this same run early-stops.
                for item in pending:
                    known.add(str(item["t"]["id"]))
            pending.clear()
        push_reply_counts()
        touch_run(partial=True)                     # partial until finalize() says otherwise

    def relabel():
        """Recompute source_type now that every auto-discovered outlet is known.

        The flush that ran mid-sweep labelled tweets with the outlets found SO FAR.
        classify() is a label, not a filter, so this only changes source_type /
        outlet_handle on rows this run inserted.
        """
        if sb is None:
            for item in collected:
                build_raw(item)                     # still fill labels so the CSV is right
            return
        changed_raw, changed_clean = [], []
        for item in collected:
            prev = (item.get("source_type"), item.get("outlet_handle"))
            raw = build_raw(item)
            if (item.get("source_type"), item.get("outlet_handle")) != prev:
                changed_raw.append(raw)
                if item["reason"] != "retweet":
                    changed_clean.append(build_clean(item))
        if changed_raw:
            upsert(sb, changed_raw, C.SUPABASE_RAW_TABLE, ignore_duplicates=False)
        if changed_clean:
            upsert(sb, changed_clean, C.SUPABASE_TABLE, ignore_duplicates=False)

    def write_csv():
        if not out_csv:
            return
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        raw_rows = [build_raw(item) for item in collected]
        cols = [k for k in raw_rows[0].keys() if k != "raw"] if raw_rows else ["tweet_id"]
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(raw_rows)                   # the CSV is the raw table: filter on drop_reason
        print(f"wrote {out_csv} ({len(raw_rows)} rows incl. retweets)")

    def enough(limit: int = target) -> bool:
        """Stop starting new queries once we have enough new rows, or the cap is hit."""
        return client.over_cap() or client.aborted or len(seen) >= limit

    # ---- Pass 1: keyword sweep ---------------------------------------------
    queries = build_queries()
    fresh, hist = time_windows(mode, now=now, watermark=prev_wm)
    news_posts: dict[str, dict] = {}                # outlet posts whose threads we may expand
    covered = load_coverage(sb) if hist else set()
    skipped = 0
    sweep_target = int(target * 0.7)                # pass 1 stops at 70%; the rest is for thread replies

    def sweep(since: int, until: int, max_pages: int, ledger: bool, label: str,
              stop_when_known: bool, phase: str | None):
        """Run every query on one time window. Flush after each query."""
        nonlocal skipped
        for qi, q in enumerate(queries):
            if enough(sweep_target) or client.aborted:
                return
            latest_full = False
            for qt in ("Latest", "Top"):            # two orderings the API offers
                if enough(sweep_target) or client.aborted:
                    return
                # "Top" returns (and charges) mostly the same tweets as "Latest" unless the window
                # was too busy for Latest to exhaust — only then is Top worth it.
                if qt == "Top" and not latest_full:
                    if ledger and not client.exhausted and not client.aborted:
                        mark_coverage(sb, since, until, qi, qt, 0)   # nothing more to fetch here
                    continue
                if ledger and is_covered(covered, since, until, qi, qt):
                    skipped += 1                    # already paid for this cell in a previous run
                    if phase and qt == "Latest":
                        done[phase].add(qi)         # not a hole; the ledger already has this cell
                    continue
                digging = dig_all or (phase == "fresh" and qi in prev_redo_fresh) or (
                    phase == "week" and qi in prev_redo_week)
                # A query the previous run did not finish may need more than the daily 2 pages.
                pages = pages_for_fresh(mode, since, until, digging) if phase == "fresh" else max_pages
                # Early-stop is safe only for a query the previous run finished. A query
                # that died mid-page must be read again, or the newest known page would
                # hide the older tweets that were never stored.
                use_stop = stop_when_known and qt == "Latest" and not digging
                stat = {"window": label, "query_idx": qi, "query_type": qt,
                        "fetched": 0, "unique_new": 0, "already_stored": 0,
                        "stopped_known_page": False, "query": q[:90]}
                full = f"{q} since_time:{since} until_time:{until}"

                def stop_fn(tweets, _use=use_stop):
                    # Read the sets at call time, after earlier pages were recorded,
                    # and before this page's ids are added.
                    if not _use:
                        return False
                    return page_all_known(tweets, known | set(seen) | rejected_ids)

                for t in client.search(full, qt, pages, stop_if_page=stop_fn if use_stop else None):
                    tid = str(t.get("id") or "")
                    stat["fetched"] += 1
                    if not tid or tid in baseline or tid in seen or tid in rejected_ids:
                        stat["already_stored"] += 1
                    else:
                        stat["unique_new"] += 1
                    un = ((t.get("author") or {}).get("userName") or "").lower()
                    if is_thread_seed(t, outlets):
                        news_posts[str(t["id"])] = t
                        outlets.add(un)             # auto-discovered outlet, for classify()
                    consider(t, full)
                stat["stopped_known_page"] = bool(client.last_page_stop)
                yields.append(stat)
                flush()                             # persist even if this query then fails
                if client.aborted or client.capped:
                    return                          # do not mark a truncated query as finished
                if client.query_failed:
                    break                           # don't mark it done, and don't try Top
                if qt == "Latest":
                    latest_full = stat["fetched"] >= pages * 20 - 2 and not client.last_page_stop
                    if phase:
                        done[phase].add(qi)
                if ledger and not client.exhausted:
                    mark_coverage(sb, since, until, qi, qt, stat["fetched"])
                    covered.add((since, until, qi, qt))
                print(f"[{label}] {qt:6} {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d} "
                      f"→ {stat['fetched']:3} fetched, {stat['unique_new']:3} new | "
                      f"{len(seen)} stored | {client.tweets_fetched} total"
                      + (" | stop: page of known ids" if client.last_page_stop else ""))

    def _run_phases():
        """Fresh window, current week (or history), then thread expansion."""
        nonlocal threads_expanded, threads_skipped
        # Phase 1 — what's new since the last finished run. Not written to the
        # ledger: the window moves every day, on purpose.
        if fresh:
            since_f, until_f = fresh
            hours = (until_f - since_f) / 3600
            print(f"fresh window: {datetime.fromtimestamp(since_f, timezone.utc):%Y-%m-%d %H:%M} → "
                  f"{datetime.fromtimestamp(until_f, timezone.utc):%Y-%m-%d %H:%M} ({hours:.1f} h)")
            if dig_all:
                print("previous daily run died before recording progress — not early-stopping this time")
            elif prev_redo_fresh or prev_redo_week:
                print(f"redoing unfinished queries fresh={sorted(prev_redo_fresh)} "
                      f"week={sorted(prev_redo_week)} without early-stop")
            sweep(since_f, until_f, pages_for_fresh(mode, since_f, until_f, dig_all),
                  ledger=False, label=f"{mode}:fresh", stop_when_known=True, phase="fresh")

        # Phase 2 — history. Daily keeps only the current partial week (index 0)
        # and stops when a Latest page is all known ids. Backfill walks every week;
        # the ledger skips cells already fetched, and we do NOT early-stop there
        # (a known newest page must not hide older tweets the ledger never marked).
        for i, (since_h, until_h) in enumerate(hist):
            if enough(sweep_target) or client.aborted:
                break
            sweep(since_h, until_h, P["max_pages_per_query_window"], ledger=(i > 0),
                  label=f"{mode}:hist", stop_when_known=(i == 0),
                  phase="week" if i == 0 else None)

        # Pass 2 — replies under outlet posts that are new or have new replies.
        threads = sorted(news_posts.values(), key=lambda t: t.get("replyCount") or 0, reverse=True)
        done_cids: set[str] = set()                 # conversation ids already expanded this run
        for t in threads:
            if threads_expanded >= P["max_threads"] or enough() or client.aborted:
                break
            cid = str(t.get("conversationId") or t["id"])
            if cid in done_cids:
                continue
            un = (t.get("author") or {}).get("userName") or ""
            if not thread_needs_expansion(t, baseline | known, reply_counts):
                threads_skipped += 1
                print(f"  thread @{un} skipped (reply_count {t.get('replyCount')} already stored)")
                continue
            done_cids.add(cid)
            threads_expanded += 1
            q = f"conversation_id:{cid} -from:{un} -filter:nativeretweets"
            stat = {"window": f"{mode}:thread", "query_idx": -1, "query_type": "Latest",
                    "fetched": 0, "unique_new": 0, "already_stored": 0, "stopped_known_page": False}
            got = 0

            def thread_stop(tweets):
                return page_all_known(tweets, known | set(seen) | rejected_ids)

            for r in client.search(q, "Latest", P["max_thread_pages"], stop_if_page=thread_stop):
                tid = str(r.get("id") or "")
                stat["fetched"] += 1
                if not tid or tid in baseline or tid in seen or tid in rejected_ids:
                    stat["already_stored"] += 1
                else:
                    stat["unique_new"] += 1
                if consider(r, q, thread_of=un.lower()) is None:
                    got += 1
            stat["stopped_known_page"] = bool(client.last_page_stop)
            yields.append(stat)
            # Remember the live reply_count so the next run skips this thread until it grows.
            seed_id = str(t.get("id") or "")
            if seed_id and seed_id in baseline:
                reply_updates.append({"tweet_id": seed_id,
                                      "reply_count": int(t.get("replyCount") or 0)})
            flush()
            print(f"  thread @{un} ({t.get('replyCount')} replies) → {got} new")
            if client.aborted:
                break

    # Anything raised in the phases is caught so tweets already pulled still get flushed.
    error = None
    try:
        _run_phases()
    except Exception as e:                          # noqa: BLE001 — save paid tweets, then report
        error = e
        print(f"  ! run failed ({type(e).__name__}: {e}); saving tweets already fetched")
        try:
            flush()
        except Exception as flush_error:            # noqa: BLE001
            print(f"  ! could not flush after the failure: {flush_error}")

    # ---- Finish: relabel, CSV, close the run row ---------------------------
    try:
        relabel()
        write_csv()
    except Exception as e:                          # noqa: BLE001 — persist path must not raise past finalize
        if error is None:
            error = e
        print(f"  ! could not finish the run ({type(e).__name__}: {e})")

    fresh_redo, week_redo = redo_lists()
    # At the cap with every query finished is still a complete window: the cap
    # did not cut a page off. A cap that stopped us mid-query leaves redo entries.
    cap_blocked = client.over_cap() and bool(fresh_redo or week_redo)
    partial = bool(error or client.aborted or cap_blocked or fresh_redo or week_redo)
    wm = prev_wm if "lookback_hours" not in P else claim_watermark(
        now, prev_wm, aborted=bool(error or client.aborted), over_cap=cap_blocked,
        redo=fresh_redo + week_redo)
    if sb is not None and run_id is not None:
        try:
            sb.table(C.SUPABASE_RUNS_TABLE).update({
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "fetched": client.tweets_fetched,
                "inserted": len(seen),
                "api_pages": client.pages,
                "est_cost_usd": round(client.est_cost(), 4),
                "notes": json.dumps(notes_payload(partial, wm, fresh_redo, week_redo)),
            }).eq("id", run_id).execute()
        except Exception as e:
            print(f"  ! could not close dc_runs: {str(e)[:120]}")
            partial = True

    counts: dict[str, int] = {}
    for item in collected:
        if item["reason"] == "retweet":
            continue
        st = item.get("source_type") or "?"
        counts[st] = counts.get(st, 0) + 1
    if any(i > 0 for i, _ in enumerate(hist)):
        print(f"coverage: skipped {skipped} already-fetched cells")
    elif hist:
        print(f"history walk off — swept the current week only; skipped {skipped} ledger cells")
    print("\nper-query unique yield (new ids / fetched). A query stuck at 0 new is a candidate to drop:")
    for s in sorted(yields, key=lambda s: (s["unique_new"], -s["fetched"])):
        print(f"  {s['unique_new']:4} new / {s['fetched']:4} fetched / {s['already_stored']:4} already stored  "
              f"[{s['window']}] {s['query_type']:6} q{s['query_idx']} {s.get('query', '')}")
    print(f"\nstored {len(seen)} new (non-retweet) | by source: {counts} | "
          f"dropped: {dropped} | threads expanded {threads_expanded}, skipped {threads_skipped}")
    print(f"API pages: {client.pages} | tweets fetched: {client.tweets_fetched} "
          f"| est. cost ${client.est_cost():.3f}")
    if sb is not None:
        print(f"rows this run: {len(collected)} into {C.SUPABASE_RAW_TABLE}, "
              f"{len(seen)} into {C.SUPABASE_TABLE}")
    if partial:
        print("run incomplete — tweets already fetched are saved; "
              "the next run will cover the queries that did not finish")
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(C.MODES), default="daily")
    ap.add_argument("--dry-run", action="store_true", help="don't touch Supabase")
    ap.add_argument("--csv", default=None, help="also write the raw rows to this CSV")
    args = ap.parse_args()
    try:
        code = run(args.mode, args.dry_run, args.csv)
    except Exception as e:
        # A bug outside the per-query flush. Re-raise so the traceback is visible;
        # tweets flushed before the exception are already in Supabase.
        print(f"  ! uncaught {type(e).__name__}: {e}")
        raise
    sys.exit(code)
