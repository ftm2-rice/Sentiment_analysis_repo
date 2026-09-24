#!/usr/bin/env python3
"""
Collect public opinion tweets about data centers in Argentina and store them
in Supabase.

    python scraper/scrape.py --mode backfill   # one-off, ~3,000 tweets, last 12 months
    python scraper/scrape.py --mode daily      # cron, ~100 new tweets
    python scraper/scrape.py --mode daily --dry-run   # fetch, don't write, dump CSV

Env vars:  TWITTERAPI_IO_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402

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

    def over_cap(self) -> bool:
        return self.tweets_fetched >= self.hard_cap

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
# Helpers
# ---------------------------------------------------------------------------
def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def visible_text(t: dict) -> str:
    """Text with leading @mentions stripped (what the reader actually sees)."""
    txt = t.get("text") or ""
    rng = t.get("displayTextRange")
    if isinstance(rng, list) and len(rng) == 2:
        try:
            txt = txt[rng[0]:rng[1]]
        except Exception:
            pass
    txt = re.sub(r"https?://\S+", "", txt)
    return txt.strip()


def is_bot(author: dict) -> str | None:
    """Return the rule name that fired, or None."""
    R = C.BOT_RULES
    if R["drop_if_automated_flag"] and author.get("isAutomated"):
        return "automated_flag"
    un = author.get("userName") or ""
    if re.search(R["drop_if_username_matches"], un):
        return "username_pattern"
    created = parse_dt(author.get("createdAt"))
    if created:
        age = max((datetime.now(timezone.utc) - created).days, 1)
        if age < R["min_account_age_days"]:
            return "account_too_new"
        if (author.get("statusesCount") or 0) / age > R["max_statuses_per_day"]:
            return "posting_rate"
    return None


def author_is_verified(a: dict) -> bool:
    return bool(a.get("isBlueVerified")) or bool(a.get("verifiedType"))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(t: dict, outlets: set[str]) -> tuple[str, str | None]:
    """Return (source_type, outlet_handle)."""
    a = t.get("author") or {}
    if (a.get("userName") or "").lower() in outlets and not t.get("isReply"):
        return "outlet_post", (a.get("userName") or "").lower()   # headline, not opinion
    reply_to = (t.get("inReplyToUsername") or "").lower()
    if t.get("isReply") and reply_to in outlets:
        return "news_reply", reply_to
    q = t.get("quoted_tweet") or {}
    q_user = ((q.get("author") or {}).get("userName") or "").lower()
    if q_user and q_user in outlets:
        return "news_quote", q_user
    if author_is_verified(a) and (a.get("followers") or 0) >= C.VERIFIED_MIN_FOLLOWERS:
        return "verified_opinion", None
    return "general_public", None


def to_row(t: dict, source_type: str, outlet: str | None, query: str, mode: str) -> dict:
    a = t.get("author") or {}
    q = t.get("quoted_tweet") or {}
    return {
        "tweet_id": str(t["id"]),
        "created_at": (parse_dt(t.get("createdAt")) or datetime.now(timezone.utc)).isoformat(),
        "text": t.get("text") or "",
        "lang": t.get("lang"),
        "url": t.get("url"),
        "author_id": a.get("id"),
        "author_username": a.get("userName"),
        "author_name": a.get("name"),
        "author_followers": a.get("followers"),
        "author_following": a.get("following"),
        "author_statuses": a.get("statusesCount"),
        "author_created_at": (parse_dt(a.get("createdAt")) or None) and parse_dt(a.get("createdAt")).isoformat(),
        "author_verified": author_is_verified(a),
        "author_verified_type": a.get("verifiedType") or None,
        "author_is_automated": bool(a.get("isAutomated")),
        "author_description": a.get("description"),
        "author_location": a.get("location"),
        "like_count": t.get("likeCount"),
        "retweet_count": t.get("retweetCount"),
        "reply_count": t.get("replyCount"),
        "quote_count": t.get("quoteCount"),
        "view_count": t.get("viewCount"),
        "is_reply": bool(t.get("isReply")),
        "in_reply_to_id": t.get("inReplyToId") or None,
        "in_reply_to_username": t.get("inReplyToUsername") or None,
        "conversation_id": t.get("conversationId") or None,
        "quoted_tweet_id": q.get("id"),
        "quoted_username": (q.get("author") or {}).get("userName"),
        "source_type": source_type,
        "outlet_handle": outlet,
        "matched_query": query[:500],
        "run_mode": mode,
        "raw": t,
    }


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
def get_supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def existing_ids(sb) -> set[str]:
    ids, start, step = set(), 0, 1000
    while True:
        res = sb.table(C.SUPABASE_TABLE).select("tweet_id").range(start, start + step - 1).execute()
        data = res.data or []
        ids.update(r["tweet_id"] for r in data)
        if len(data) < step:
            return ids
        start += step


def upsert(sb, rows: list[dict]) -> int:
    n = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        sb.table(C.SUPABASE_TABLE).upsert(chunk, on_conflict="tweet_id", ignore_duplicates=True).execute()
        n += len(chunk)
    return n


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


def time_windows(mode: str) -> list[tuple[int, int]]:
    now = datetime.now(timezone.utc)
    if mode == "daily":
        start = now - timedelta(hours=C.DAILY["lookback_hours"])
        return [(int(start.timestamp()), int(now.timestamp()))]
    wins, end = [], now
    for _ in range(C.BACKFILL["months_back"]):
        start = end - timedelta(days=30)
        wins.append((int(start.timestamp()), int(end.timestamp())))
        end = start
    return wins


def run(mode: str, dry_run: bool, out_csv: str | None):
    P = C.DAILY if mode == "daily" else C.BACKFILL
    client = Client(os.environ["TWITTERAPI_IO_KEY"], P["hard_cap_tweets_fetched"])
    outlets = {h.lower() for h in C.OUTLETS}

    sb = None
    known: set[str] = set()
    if not dry_run:
        sb = get_supabase()
        known = existing_ids(sb)
        print(f"{len(known)} tweets already stored")
        run_row = sb.table(C.SUPABASE_RUNS_TABLE).insert({"run_mode": mode}).execute()
        run_id = run_row.data[0]["id"]

    seen: dict[str, dict] = {}       # tweet_id -> raw
    seen_query: dict[str, str] = {}
    dropped = {"retweet": 0, "bot": 0, "short": 0, "known": 0}

    def consider(t: dict, query: str) -> bool:
        tid = str(t.get("id") or "")
        if not tid or tid in seen:
            return False
        if t.get("retweeted_tweet"):
            dropped["retweet"] += 1
            return False
        if tid in known:
            dropped["known"] += 1
            return False
        a = t.get("author") or {}
        if is_bot(a):
            dropped["bot"] += 1
            return False
        if len(visible_text(t)) < C.BOT_RULES["min_text_chars"]:
            dropped["short"] += 1
            return False
        seen[tid] = t
        seen_query[tid] = query
        return True

    # ---- Pass 1: keyword sweep --------------------------------------------
    queries = build_queries()
    windows = time_windows(mode)
    max_pages = P.get("max_pages_per_query_window", P.get("max_pages_per_query", 3))
    news_posts: dict[str, dict] = {}   # candidate threads to expand

    for (since, until) in windows:
        if client.over_cap() or len(seen) >= P["target_new_tweets"] * 1.5:
            break
        for q in queries:
            for qt in ("Latest", "Top"):
                if client.over_cap():
                    break
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
                            and not t.get("isReply"):
                        news_posts[str(t["id"])] = t
                        outlets.add(un)         # auto-discovered outlet
                    consider(t, full)
                print(f"[{mode}] {qt:6} {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d} "
                      f"→ {got:3} fetched | {len(seen)} kept | {client.tweets_fetched} total")
        if mode == "daily":
            break

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
        if client.over_cap() or len(seen) >= P["target_new_tweets"] * 1.5:
            break
        cid = t.get("conversationId") or t["id"]
        un = (t.get("author") or {}).get("userName") or ""
        q = f"conversation_id:{cid} -from:{un} -filter:nativeretweets"
        got = sum(1 for r in client.search(q, "Latest", P["max_thread_pages"]) if consider(r, q))
        print(f"  thread @{un} ({t.get('replyCount')} replies) → {got} kept")

    # ---- Classify, trim, write --------------------------------------------
    rows = [to_row(t, *classify(t, outlets), seen_query[tid], mode) for tid, t in seen.items()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    rows = rows[:P["target_new_tweets"]] if mode == "daily" else rows

    counts = {}
    for r in rows:
        counts[r["source_type"]] = counts.get(r["source_type"], 0) + 1
    print(f"\nkept {len(rows)} | by source: {counts} | dropped: {dropped}")
    print(f"API pages: {client.pages} | tweets fetched: {client.tweets_fetched} "
          f"| est. cost ${client.est_cost():.3f}")

    if out_csv:
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        cols = [k for k in rows[0].keys() if k != "raw"] if rows else ["tweet_id"]
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {out_csv}")

    inserted = 0
    if sb is not None:
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
    ap.add_argument("--mode", choices=["backfill", "daily"], default="daily")
    ap.add_argument("--dry-run", action="store_true", help="don't touch Supabase")
    ap.add_argument("--csv", default=None, help="also write rows to this CSV")
    args = ap.parse_args()
    run(args.mode, args.dry_run, args.csv)
