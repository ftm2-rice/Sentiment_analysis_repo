#!/usr/bin/env python3
"""
scrape.py — collect public tweets about data centers in Argentina and store them
in Supabase.

    python scrape.py --mode daily                 # cron: last 48 h + history until 1,000 clean rows
    python scrape.py --mode backfill              # one-off deep sweep
    python scrape.py --mode test                  # smoke test, ≤100 tweets fetched (≈ $0.015)
    python scrape.py --mode daily --dry-run --csv out/x.csv   # fetch, don't write, dump the raw CSV
    python clean.py                               # re-apply the rules from dc_tweets_raw → dc_tweets

Two passes:
    1. keyword sweep   — the queries in config.py, by time window
    2. thread expansion — replies under on-topic outlet posts found in pass 1

Every fetched tweet ends up in dc_tweets_raw with a drop_reason (NULL = kept).
The kept ones also go to dc_tweets. The rules themselves live in filters.py.

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
from filters import (parse_dt, text_key, author_is_verified, is_on_topic,  # noqa: E402
                     tweet_topic_text, content_reason, classify, to_row)

API = "https://api.twitterapi.io/twitter/tweet/advanced_search"   # the search endpoint
COST_PER_TWEET = 0.00015  # $0.15 per 1,000 tweets returned
MIN_CALL_COST = 0.00015   # each call costs at least 15 credits, even if it returns nothing


# ---------------------------------------------------------------------------
# HTTP client: talks to the API, follows pages, counts what we fetched
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, key: str, hard_cap: int):
        self.s = requests.Session()                 # reuses the connection between calls
        self.s.headers["X-API-Key"] = key           # authentication header on every request
        self.pages = 0                              # API calls made
        self.tweets_fetched = 0                     # tweets returned so far (what we pay for)
        self.hard_cap = hard_cap                    # stop fetching once we reach this many
        self.exhausted = False                      # set when the API says no credits left (402)

    def over_cap(self) -> bool:
        """True when we must stop: out of credits, or at the per-run cap."""
        return self.exhausted or self.tweets_fetched >= self.hard_cap

    def search(self, query: str, query_type: str = "Latest", max_pages: int = 3):
        """Yield tweet dicts for one query, following the cursor up to max_pages pages."""
        cursor = ""                                 # "" = first page
        for _ in range(max_pages):
            if self.over_cap():
                return
            for attempt in range(4):                # retry up to 4 times on rate limit / server error
                r = self.s.get(API, params={"query": query, "queryType": query_type,
                                            "cursor": cursor}, timeout=60)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 ** attempt)        # wait 1, 2, 4, 8 s
                    continue
                break
            if r.status_code == 402 or (r.status_code in (401, 403) and "credit" in r.text.lower()):
                print(f"  ! {r.status_code} — account out of credits, stopping all requests")
                self.exhausted = True
                return
            if r.status_code != 200:                # any other error: give up on this query
                print(f"  ! {r.status_code} on {query[:60]}…: {r.text[:200]}")
                return
            d = r.json()                            # the response body
            self.pages += 1
            tweets = d.get("tweets") or []          # this page's tweets (about 20)
            self.tweets_fetched += len(tweets)
            for t in tweets:
                yield t                             # hand each tweet to the caller
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


def existing_ids(sb) -> set[str]:
    """All tweet_ids already in dc_tweets, read 1,000 at a time."""
    ids, start, step = set(), 0, 1000
    while True:
        res = sb.table(C.SUPABASE_TABLE).select("tweet_id").range(start, start + step - 1).execute()
        data = res.data or []
        ids.update(r["tweet_id"] for r in data)
        if len(data) < step:                        # short page = last page
            return ids
        start += step


def upsert(sb, rows: list[dict], table: str = C.SUPABASE_TABLE) -> int:
    """Insert rows in batches of 200; rows whose tweet_id already exists are left untouched."""
    n = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        sb.table(table).upsert(chunk, on_conflict="tweet_id", ignore_duplicates=True).execute()
        n += len(chunk)
    return n


def load_coverage(sb) -> set[tuple]:
    """The ledger of (window_start, window_end, query_idx, query_type) cells already swept.
    Lets a later run skip weeks it has already paid for."""
    if sb is None:
        return set()
    try:
        res = sb.table(C.SUPABASE_COVERAGE_TABLE).select("window_start,window_end,query_idx,query_type").execute()
    except Exception as e:  # table missing → behave as if nothing were covered
        print(f"  ! coverage table unavailable ({str(e)[:80]}); every window will be fetched")
        return set()
    out = set()
    for r in res.data or []:
        ws = parse_dt(r["window_start"]); we = parse_dt(r["window_end"])
        out.add((int(ws.timestamp()), int(we.timestamp()), int(r["query_idx"]), r["query_type"]))
    return out


def is_covered(covered: set[tuple], since: int, until: int, qi: int, qt: str) -> bool:
    """True if this exact cell was swept, or a larger window containing it was."""
    if (since, until, qi, qt) in covered:
        return True
    return any(ws <= since and we >= until and cqi == qi and cqt == qt
               for ws, we, cqi, cqt in covered)


def mark_coverage(sb, since: int, until: int, qi: int, qt: str, fetched: int):
    """Record a swept cell in the ledger."""
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


def time_windows(mode: str) -> tuple[tuple[int, int] | None, list[tuple[int, int]]]:
    """Return (fresh, hist):
    fresh = (start, end) Unix timestamps for the last N hours, or None
    hist  = ISO-week windows, newest first, back to HISTORY_START.
    Weeks start on Monday 00:00 UTC so the same cells recur run after run and the ledger can skip them."""
    P = C.MODES[mode]
    now = datetime.now(timezone.utc)
    fresh = None
    if "lookback_hours" in P:                       # only the daily/test modes have a fresh phase
        start = now - timedelta(hours=P["lookback_hours"])
        fresh = (int(start.timestamp()), int(now.timestamp()))
    hist = []
    if P.get("start_date"):
        floor = datetime.fromisoformat(P["start_date"]).replace(tzinfo=timezone.utc)   # don't go earlier than this
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        hist.append((int(monday.timestamp()), int(now.timestamp())))     # the current, partial week
        end = monday
        while end > floor:                          # walk back one week at a time
            start = end - timedelta(days=C.WINDOW_DAYS)
            hist.append((int(max(start, floor).timestamp()), int(end.timestamp())))
            end = start
    return fresh, hist


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def run(mode: str, dry_run: bool, out_csv: str | None):
    P = C.MODES[mode]                               # the caps for this mode (daily / test / …)
    client = Client(os.environ["TWITTERAPI_IO_KEY"], P["hard_cap_tweets_fetched"])
    outlets = {h.lower() for h in C.OUTLETS}        # lowercase handles; grows with auto-discovered outlets

    # ---- Budget guard and run bookkeeping (skipped on --dry-run) ----------
    sb = None
    run_id = None
    known: set[str] = set()                         # tweet_ids already in dc_tweets
    if not dry_run:
        sb = get_supabase()
        budget = float(os.environ.get("BUDGET_USD", C.BUDGET_USD))
        spent = spent_so_far(sb)
        print(f"spent so far ≈ ${spent:.2f} of ${budget:.2f} budget")
        if spent >= budget:
            print("budget reached — not running. Raise BUDGET_USD (env or config) after recharging.")
            return
        remaining_tweets = int((budget - spent) / COST_PER_TWEET)   # how many more tweets the budget allows
        client.hard_cap = min(client.hard_cap, remaining_tweets)    # never overshoot it
        known = existing_ids(sb)
        print(f"{len(known)} tweets already stored")
        run_row = sb.table(C.SUPABASE_RUNS_TABLE).insert({"run_mode": mode}).execute()   # one row per run
        run_id = run_row.data[0]["id"]

    # ---- State for this run -------------------------------------------------
    seen: dict[str, dict] = {}        # tweet_id → tweet, for the ones we keep
    seen_query: dict[str, str] = {}   # tweet_id → the query that found it
    seen_text: set[str] = set()       # normalised texts kept so far (dedupe)
    seen_thread: dict[str, str] = {}  # tweet_id → outlet handle, for tweets found by thread expansion
    rejected: list[tuple[dict, str, str, str | None]] = []   # (tweet, reason, query, thread_of) — kept for the raw table
    dropped = {"retweet": 0, "bot": 0, "short": 0, "offtopic": 0, "dup_text": 0, "known": 0}   # counters

    def consider(t: dict, query: str, thread_reply: bool = False, thread_of: str | None = None) -> bool:
        """Judge one fetched tweet. Returns True if kept. Rejects are remembered with
        their reason so the raw table shows what was fetched, not only what survived."""
        tid = str(t.get("id") or "")
        if not tid or tid in seen:
            return False                            # no id, or already kept in this run
        if tid in known:
            dropped["known"] += 1                   # already in dc_tweets from an earlier run: nothing new
            return False
        reason = content_reason(t, outlets, thread_reply)          # rules 1–2 (filters.py)
        if reason is None and text_key(t) in seen_text:            # dedupe: same text as a kept tweet
            reason = "dup_text"
        if reason:
            dropped[reason] += 1
            rejected.append((t, reason, query, thread_of))         # keep the reject
            return False
        seen_text.add(text_key(t))
        if thread_of:
            seen_thread[tid] = thread_of            # remember which outlet's thread it came from
        seen[tid] = t
        seen_query[tid] = query
        return True

    # ---- Pass 1: keyword sweep ---------------------------------------------
    queries = build_queries()
    fresh, hist = time_windows(mode)
    news_posts: dict[str, dict] = {}                # outlet posts whose threads we'll expand in pass 2
    covered = load_coverage(sb) if hist else set()  # ledger of cells already swept
    skipped = 0
    target = P["target_new_tweets"]                 # clean rows wanted this run
    sweep_target = int(target * 0.7)                # pass 1 stops at 70 %; the rest is for thread replies

    def enough(limit: int = target) -> bool:
        return client.over_cap() or len(seen) >= limit

    def sweep(since: int, until: int, max_pages: int, ledger: bool, label: str):
        """Run every query on one time window."""
        nonlocal skipped
        for qi, q in enumerate(queries):
            latest_full = False
            for qt in ("Latest", "Top"):            # two orderings the API offers
                if enough(sweep_target):
                    return
                # "Top" returns (and charges) mostly the same tweets as "Latest" unless the window
                # was too busy for Latest to exhaust — only then is Top worth it.
                if qt == "Top" and not latest_full:
                    if ledger and not client.exhausted:
                        mark_coverage(sb, since, until, qi, qt, 0)   # nothing more to fetch here
                    continue
                if ledger and is_covered(covered, since, until, qi, qt):
                    skipped += 1                    # already paid for this cell in a previous run
                    continue
                full = f"{q} since_time:{since} until_time:{until}"   # the query with its time window
                got = 0
                for t in client.search(full, qt, max_pages):
                    got += 1
                    a = t.get("author") or {}
                    un = (a.get("userName") or "").lower()
                    # Thread candidate: an outlet (or a big verified account) whose on-topic post drew replies.
                    if (un in outlets or
                        (author_is_verified(a)
                         and (a.get("followers") or 0) >= C.AUTO_OUTLET_MIN_FOLLOWERS)) \
                            and (t.get("replyCount") or 0) >= C.AUTO_OUTLET_MIN_REPLIES \
                            and not t.get("isReply") \
                            and is_on_topic(tweet_topic_text(t)):
                        news_posts[str(t["id"])] = t
                        outlets.add(un)             # auto-discovered outlet
                    consider(t, full)               # judge and record
                if qt == "Latest":
                    latest_full = got >= max_pages * 20 - 2   # every page came back full → window is busy
                if ledger and not client.exhausted:
                    mark_coverage(sb, since, until, qi, qt, got)
                print(f"[{label}] {qt:6} {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d} "
                      f"→ {got:3} fetched | {len(seen)} kept | {client.tweets_fetched} total")

    # Phase 1 — what's new in the last N hours (never recorded in the ledger: it overlaps on purpose)
    if fresh:
        sweep(*fresh, P.get("fresh_pages_per_query", 3), ledger=False, label=f"{mode}:fresh")

    # Phase 2 — history, newest week first; the ledger skips cells already fetched.
    # The current partial week (i == 0) is re-swept but never marked done.
    for i, (since, until) in enumerate(hist):
        if enough(sweep_target):
            break
        sweep(since, until, P["max_pages_per_query_window"], ledger=(i > 0), label=f"{mode}:hist")

    # ---- Pass 2: expand the discussion under news posts ---------------------
    threads = sorted(news_posts.values(), key=lambda t: t.get("replyCount") or 0, reverse=True)   # busiest first
    done_cids: set[str] = set()                     # conversation ids already expanded
    for t in threads:
        if len(done_cids) >= P["max_threads"]:
            break
        cid0 = t.get("conversationId") or t["id"]
        if cid0 in done_cids:
            continue
        done_cids.add(cid0)
        if enough():
            break
        cid = t.get("conversationId") or t["id"]
        un = (t.get("author") or {}).get("userName") or ""
        q = f"conversation_id:{cid} -from:{un} -filter:nativeretweets"   # replies in the thread, not the outlet's own
        got = sum(1 for r in client.search(q, "Latest", P["max_thread_pages"])
                  if consider(r, q, thread_reply=True, thread_of=un.lower()))   # thread replies skip the topic check
        print(f"  thread @{un} ({t.get('replyCount')} replies) → {got} kept")

    # ---- Classify, trim, build the rows --------------------------------------
    rows = []
    for tid, t in seen.items():
        st, oh = classify(t, outlets, thread_of=seen_thread.get(tid))   # rule 3: segment
        rows.append(to_row(t, st, oh, seen_query[tid], mode))
    rows.sort(key=lambda r: r["created_at"], reverse=True)              # newest first
    kept, over = rows[:P["target_new_tweets"]], rows[P["target_new_tweets"]:]   # cap at the target

    # The raw table = everything fetched this run: survivors with drop_reason NULL,
    # rows beyond the cap as "over_target", rejects with the rule that fired.
    def raw_row(r: dict, reason: str | None, thread_of: str | None) -> dict:
        return {**r, "drop_reason": reason, "found_via": "thread" if thread_of else "search",
                "thread_of": thread_of, "run_id": run_id, "legacy": False}

    raw_rows = [raw_row(r, None, seen_thread.get(r["tweet_id"])) for r in kept]
    raw_rows += [raw_row(r, "over_target", seen_thread.get(r["tweet_id"])) for r in over]
    for t, reason, q, thread_of in rejected:
        st, oh = classify(t, outlets, thread_of=thread_of)              # segment even for rejects (useful in the notebook)
        raw_rows.append(raw_row(to_row(t, st, oh, q, mode), reason, thread_of))
    rows = kept

    # ---- Report ----------------------------------------------------------------
    counts = {}
    for r in rows:
        counts[r["source_type"]] = counts.get(r["source_type"], 0) + 1   # kept rows per segment
    if hist:
        print(f"coverage: skipped {skipped} already-fetched cells")
    print(f"\nkept {len(rows)} | by source: {counts} | dropped: {dropped} | over target: {len(over)}")
    print(f"API pages: {client.pages} | tweets fetched: {client.tweets_fetched} "
          f"| est. cost ${client.est_cost():.3f}")

    # ---- Write: CSV (optional) and Supabase -----------------------------------
    if out_csv:
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        cols = [k for k in raw_rows[0].keys() if k != "raw"] if raw_rows else ["tweet_id"]   # all columns but the JSON blob
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(raw_rows)                   # the CSV is the raw table: filter on drop_reason
        print(f"wrote {out_csv} ({len(raw_rows)} rows incl. rejects)")

    inserted = 0
    if sb is not None:
        n_raw = upsert(sb, raw_rows, C.SUPABASE_RAW_TABLE)       # everything, with reasons
        print(f"upserted {n_raw} rows into {C.SUPABASE_RAW_TABLE}")
        inserted = upsert(sb, rows)                              # survivors only (same as before)
        sb.table(C.SUPABASE_RUNS_TABLE).update({                 # close the run's bookkeeping row
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "fetched": client.tweets_fetched,
            "inserted": inserted,
            "api_pages": client.pages,
            "est_cost_usd": round(client.est_cost(), 4),
            "notes": json.dumps({"by_source": counts, "dropped": dropped,
                                 "auto_outlets": sorted(outlets - {h.lower() for h in C.OUTLETS})}),
        }).eq("id", run_id).execute()
        print(f"upserted {inserted} rows into {C.SUPABASE_TABLE}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(C.MODES), default="daily")
    ap.add_argument("--dry-run", action="store_true", help="don't touch Supabase")
    ap.add_argument("--csv", default=None, help="also write the raw rows to this CSV")
    args = ap.parse_args()
    run(args.mode, args.dry_run, args.csv)
