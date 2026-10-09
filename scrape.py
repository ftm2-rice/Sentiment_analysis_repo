#!/usr/bin/env python3
"""
Collect public opinion tweets about data centers in Argentina and store them
in Supabase.

    python scraper/scrape.py --mode backfill   # one-off, ~3,000 tweets, last 12 months
    python scraper/scrape.py --mode daily      # cron, ~100 new tweets
    python scraper/scrape.py --mode test       # smoke test, ≤100 tweets fetched
    python scrape.py --mode daily --dry-run --csv out/x.csv   # fetch, don't write, dump raw CSV
    python clean.py                             # re-apply the rules from dc_tweets_raw → dc_tweets

Env vars:  TWITTERAPI_IO_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
from filters import (parse_dt, text_key, author_is_verified, is_on_topic,  # noqa: E402
                     tweet_topic_text, content_reason, classify, to_row)

API = "https://api.twitterapi.io/twitter/tweet/advanced_search"
COST_PER_TWEET = 0.00015  # $0.15 / 1k
MIN_CALL_COST = 0.00015   # 15 credits minimum per call


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, key: str, hard_cap: int):
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = key
        self.pages = 0
        self.tweets_fetched = 0
        self.hard_cap = hard_cap
        self.exhausted = False          # set when the API says no credits left (402)

    def over_cap(self) -> bool:
        return self.exhausted or self.tweets_fetched >= self.hard_cap

    def search(self, query: str, query_type: str = "Latest", max_pages: int = 3):
        """Yield raw tweet dicts, following the cursor."""
        cursor = ""
        for _ in range(max_pages):
            if self.over_cap():
                return
            for attempt in range(4):
                r = self.s.get(API, params={"query": query, "queryType": query_type,
                                            "cursor": cursor}, timeout=60)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                break
            if r.status_code == 402 or (r.status_code in (401, 403) and "credit" in r.text.lower()):
                print(f"  ! {r.status_code} — account out of credits, stopping all requests")
                self.exhausted = True
                return
            if r.status_code != 200:
                print(f"  ! {r.status_code} on {query[:60]}…: {r.text[:200]}")
                return
            d = r.json()
            self.pages += 1
            tweets = d.get("tweets") or []
            self.tweets_fetched += len(tweets)
            for t in tweets:
                yield t
            if not d.get("has_next_page") or not tweets:
                return
            cursor = d.get("next_cursor") or ""
            if not cursor:
                return

    def est_cost(self) -> float:
        return max(self.tweets_fetched * COST_PER_TWEET, self.pages * MIN_CALL_COST)


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
def get_supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def spent_so_far(sb) -> float:
    try:
        res = sb.table(C.SUPABASE_RUNS_TABLE).select("est_cost_usd").execute()
        return float(sum(float(r["est_cost_usd"] or 0) for r in (res.data or [])))
    except Exception:
        return 0.0


def existing_ids(sb) -> set[str]:
    ids, start, step = set(), 0, 1000
    while True:
        res = sb.table(C.SUPABASE_TABLE).select("tweet_id").range(start, start + step - 1).execute()
        data = res.data or []
        ids.update(r["tweet_id"] for r in data)
        if len(data) < step:
            return ids
        start += step


def upsert(sb, rows: list[dict], table: str = C.SUPABASE_TABLE) -> int:
    n = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        sb.table(table).upsert(chunk, on_conflict="tweet_id", ignore_duplicates=True).execute()
        n += len(chunk)
    return n


def load_coverage(sb) -> set[tuple]:
    """Cells (window_start_iso, window_end_iso, query_idx, query_type) already fetched."""
    if sb is None:
        return set()
    try:
        res = sb.table(C.SUPABASE_COVERAGE_TABLE).select("window_start,window_end,query_idx,query_type").execute()
    except Exception as e:  # table missing → behave as before
        print(f"  ! coverage table unavailable ({str(e)[:80]}); every window will be fetched")
        return set()
    out = set()
    for r in res.data or []:
        ws = parse_dt(r["window_start"]); we = parse_dt(r["window_end"])
        out.add((int(ws.timestamp()), int(we.timestamp()), int(r["query_idx"]), r["query_type"]))
    return out


def is_covered(covered: set[tuple], since: int, until: int, qi: int, qt: str) -> bool:
    """Exact cell match, or the cell lies inside a larger window already done
    (e.g. a whole month marked done covers each of its weeks)."""
    if (since, until, qi, qt) in covered:
        return True
    return any(ws <= since and we >= until and cqi == qi and cqt == qt
               for ws, we, cqi, cqt in covered)


def mark_coverage(sb, since: int, until: int, qi: int, qt: str, fetched: int):
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
# Main pipeline
# ---------------------------------------------------------------------------
def build_queries() -> list[str]:
    out = []
    n = C.TO_OUTLETS_PER_QUERY
    chunks = [C.OUTLETS[i:i + n] for i in range(0, len(C.OUTLETS), n)]
    for q in C.SEARCH_QUERIES:
        if "{to_outlets}" in q:
            for ch in chunks:
                out.append(q.format(core=C.CORE_TERMS, ar=C.ARGENTINA_ANCHORS, co=C.COMPANY_ANCHORS,
                                    to_outlets=" OR ".join(f"to:{h}" for h in ch)))
        else:
            out.append(q.format(core=C.CORE_TERMS, ar=C.ARGENTINA_ANCHORS, co=C.COMPANY_ANCHORS,
                                to_outlets=""))
    for q in out:
        assert len(q) + 45 <= 512, f"query too long ({len(q)}): {q[:80]}…"
    return out


def time_windows(mode: str) -> tuple[tuple[int, int] | None, list[tuple[int, int]]]:
    """(fresh window for the last N hours or None, ISO-week windows newest→oldest
    down to C.HISTORY_START). Week boundaries are fixed Mondays 00:00 UTC, so the
    same cells recur from run to run and the coverage ledger can skip them."""
    P = C.MODES[mode]
    now = datetime.now(timezone.utc)
    fresh = None
    if "lookback_hours" in P:
        start = now - timedelta(hours=P["lookback_hours"])
        fresh = (int(start.timestamp()), int(now.timestamp()))
    hist = []
    if P.get("start_date"):
        floor = datetime.fromisoformat(P["start_date"]).replace(tzinfo=timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        hist.append((int(monday.timestamp()), int(now.timestamp())))     # current partial week
        end = monday
        while end > floor:
            start = end - timedelta(days=C.WINDOW_DAYS)
            hist.append((int(max(start, floor).timestamp()), int(end.timestamp())))
            end = start
    return fresh, hist


def run(mode: str, dry_run: bool, out_csv: str | None):
    P = C.MODES[mode]
    client = Client(os.environ["TWITTERAPI_IO_KEY"], P["hard_cap_tweets_fetched"])
    outlets = {h.lower() for h in C.OUTLETS}

    sb = None
    run_id = None
    known: set[str] = set()
    if not dry_run:
        sb = get_supabase()
        budget = float(os.environ.get("BUDGET_USD", C.BUDGET_USD))
        spent = spent_so_far(sb)
        print(f"spent so far ≈ ${spent:.2f} of ${budget:.2f} budget")
        if spent >= budget:
            print("budget reached — not running. Raise BUDGET_USD (env or config) after recharging.")
            return
        # never let this run overshoot the budget either
        remaining_tweets = int((budget - spent) / COST_PER_TWEET)
        client.hard_cap = min(client.hard_cap, remaining_tweets)
        known = existing_ids(sb)
        print(f"{len(known)} tweets already stored")
        run_row = sb.table(C.SUPABASE_RUNS_TABLE).insert({"run_mode": mode}).execute()
        run_id = run_row.data[0]["id"]

    seen: dict[str, dict] = {}       # tweet_id -> raw
    seen_query: dict[str, str] = {}
    seen_text: set[str] = set()      # normalized text → catches copy-paste reposts
    seen_thread: dict[str, str] = {}  # tweet_id -> outlet handle, for tweets found via thread expansion
    rejected: list[tuple[dict, str, str, str | None]] = []   # (raw, reason, query, thread_of)
    dropped = {"retweet": 0, "bot": 0, "short": 0, "offtopic": 0, "dup_text": 0, "known": 0}

    def consider(t: dict, query: str, thread_reply: bool = False, thread_of: str | None = None) -> bool:
        """Apply the rules in filters.py. Rejects are kept (with their reason) so
        the raw table shows what was fetched, not only what survived."""
        tid = str(t.get("id") or "")
        if not tid or tid in seen:
            return False
        if tid in known:                       # already stored by an earlier run: nothing new to record
            dropped["known"] += 1
            return False
        reason = content_reason(t, outlets, thread_reply)
        if reason is None and text_key(t) in seen_text:   # text-only: reposts by other accounts collapse
            reason = "dup_text"
        if reason:
            dropped[reason] += 1
            rejected.append((t, reason, query, thread_of))
            return False
        seen_text.add(text_key(t))
        if thread_of:
            seen_thread[tid] = thread_of
        seen[tid] = t
        seen_query[tid] = query
        return True

    # ---- Pass 1: keyword sweep --------------------------------------------
    queries = build_queries()
    fresh, hist = time_windows(mode)
    news_posts: dict[str, dict] = {}   # candidate threads to expand
    covered = load_coverage(sb) if hist else set()
    skipped = 0
    target = P["target_new_tweets"]

    sweep_target = int(target * 0.7)    # leave ~30% of the target for thread replies

    def enough(limit: int = target) -> bool:
        return client.over_cap() or len(seen) >= limit

    def sweep(since: int, until: int, max_pages: int, ledger: bool, label: str):
        nonlocal skipped
        for qi, q in enumerate(queries):
            latest_full = False
            for qt in ("Latest", "Top"):
                if enough(sweep_target):
                    return
                # "Top" re-returns (and re-charges) the same tweets as "Latest" unless
                # the window was too busy for Latest to exhaust — only then is Top worth it.
                if qt == "Top" and not latest_full:
                    if ledger and not client.exhausted:
                        mark_coverage(sb, since, until, qi, qt, 0)   # nothing more to fetch here
                    continue
                if ledger and is_covered(covered, since, until, qi, qt):
                    skipped += 1
                    continue
                full = f"{q} since_time:{since} until_time:{until}"
                got = 0
                for t in client.search(full, qt, max_pages):
                    got += 1
                    a = t.get("author") or {}
                    un = (a.get("userName") or "").lower()
                    # thread candidates: outlets, or big verified accounts whose post drew replies
                    if (un in outlets or
                        (author_is_verified(a)
                         and (a.get("followers") or 0) >= C.AUTO_OUTLET_MIN_FOLLOWERS)) \
                            and (t.get("replyCount") or 0) >= C.AUTO_OUTLET_MIN_REPLIES \
                            and not t.get("isReply") \
                            and is_on_topic(tweet_topic_text(t)):
                        news_posts[str(t["id"])] = t
                        outlets.add(un)         # auto-discovered outlet
                    consider(t, full)
                if qt == "Latest":
                    latest_full = got >= max_pages * 20 - 2   # every page came back full
                if ledger and not client.exhausted:
                    mark_coverage(sb, since, until, qi, qt, got)
                print(f"[{label}] {qt:6} {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d} "
                      f"→ {got:3} fetched | {len(seen)} kept | {client.tweets_fetched} total")

    # Phase 1 — what's new (never recorded in the ledger)
    if fresh:
        sweep(*fresh, P.get("fresh_pages_per_query", 3), ledger=False, label=f"{mode}:fresh")

    # Phase 2 — history, newest month first; ledger skips cells already fetched.
    # The current partial month is re-swept but never marked done.
    for i, (since, until) in enumerate(hist):
        if enough(sweep_target):
            break
        sweep(since, until, P["max_pages_per_query_window"], ledger=(i > 0), label=f"{mode}:hist")

    # ---- Pass 2: expand discussion under news posts -----------------------
    threads = sorted(news_posts.values(), key=lambda t: t.get("replyCount") or 0, reverse=True)
    done_cids: set[str] = set()
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
        q = f"conversation_id:{cid} -from:{un} -filter:nativeretweets"
        got = sum(1 for r in client.search(q, "Latest", P["max_thread_pages"])
                  if consider(r, q, thread_reply=True, thread_of=un.lower()))
        print(f"  thread @{un} ({t.get('replyCount')} replies) → {got} kept")

    # ---- Classify, trim, write --------------------------------------------
    rows = []
    for tid, t in seen.items():
        st, oh = classify(t, outlets, thread_of=seen_thread.get(tid))
        rows.append(to_row(t, st, oh, seen_query[tid], mode))
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    kept, over = rows[:P["target_new_tweets"]], rows[P["target_new_tweets"]:]

    # Everything fetched this run goes to the raw table: survivors with
    # drop_reason NULL, rejects with the rule that fired. clean.py works from here.
    def raw_row(r: dict, reason: str | None, thread_of: str | None) -> dict:
        return {**r, "drop_reason": reason, "found_via": "thread" if thread_of else "search",
                "thread_of": thread_of, "run_id": run_id, "legacy": False}

    raw_rows = [raw_row(r, None, seen_thread.get(r["tweet_id"])) for r in kept]
    raw_rows += [raw_row(r, "over_target", seen_thread.get(r["tweet_id"])) for r in over]
    for t, reason, q, thread_of in rejected:
        st, oh = classify(t, outlets, thread_of=thread_of)
        raw_rows.append(raw_row(to_row(t, st, oh, q, mode), reason, thread_of))
    rows = kept

    counts = {}
    for r in rows:
        counts[r["source_type"]] = counts.get(r["source_type"], 0) + 1
    if hist:
        print(f"coverage: skipped {skipped} already-fetched cells")
    print(f"\nkept {len(rows)} | by source: {counts} | dropped: {dropped} | over target: {len(over)}")
    print(f"API pages: {client.pages} | tweets fetched: {client.tweets_fetched} "
          f"| est. cost ${client.est_cost():.3f}")

    if out_csv:
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        cols = [k for k in raw_rows[0].keys() if k != "raw"] if raw_rows else ["tweet_id"]
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(raw_rows)          # the CSV is the raw table: use the drop_reason column
        print(f"wrote {out_csv} ({len(raw_rows)} rows incl. rejects)")

    inserted = 0
    if sb is not None:
        n_raw = upsert(sb, raw_rows, C.SUPABASE_RAW_TABLE)
        print(f"upserted {n_raw} rows into {C.SUPABASE_RAW_TABLE}")
        inserted = upsert(sb, rows)
        sb.table(C.SUPABASE_RUNS_TABLE).update({
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
    ap.add_argument("--csv", default=None, help="also write rows to this CSV")
    args = ap.parse_args()
    run(args.mode, args.dry_run, args.csv)
