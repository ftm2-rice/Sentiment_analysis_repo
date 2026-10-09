#!/usr/bin/env python3
"""
Re-apply the cleaning rules to everything ever fetched (dc_tweets_raw) and
rebuild the clean table (dc_tweets) from it.

    python clean.py                     # Supabase → Supabase, writes out/pipeline_summary.json
    python clean.py --dry-run           # compute and print the funnel, write nothing
    python clean.py --csv raw.csv       # read a CSV export of dc_tweets_raw instead of Supabase
                                        # (what the notebook does); never writes to Supabase

Why this exists: scrape.py applies the same rules while collecting (it has to,
to steer thread expansion and its per-run target), but it keeps the rejects in
dc_tweets_raw with the rule that fired. That makes every rule re-runnable:
change a regex in config.py, run clean.py, and the clean table follows.

Env vars (Supabase mode): SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
from filters import CONTENT_REASONS, content_reason, classify, text_key  # noqa: E402


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
def load_raw_supabase(sb) -> list[dict]:
    rows, start, step = [], 0, 1000
    while True:
        res = sb.table(C.SUPABASE_RAW_TABLE).select("*").range(start, start + step - 1).execute()
        data = res.data or []
        rows.extend(data)
        if len(data) < step:
            return rows
        start += step


def load_raw_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("drop_reason", "thread_of", "outlet_handle", "source_type", "found_via"):
            if r.get(k) == "":
                r[k] = None
    return rows


def tweet_dict(r: dict) -> dict:
    """The API-shaped dict the rules expect. Prefer the stored `raw` JSON; fall
    back to rebuilding it from the flat columns (loses quoted-tweet text)."""
    raw = r.get("raw")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict) and raw.get("text") is not None:
        return raw
    truthy = lambda v: str(v).lower() in ("true", "1", "t")  # noqa: E731
    return {
        "id": r["tweet_id"],
        "text": r.get("text") or "",
        "createdAt": r.get("created_at"),
        "isReply": truthy(r.get("is_reply")),
        "inReplyToUsername": r.get("in_reply_to_username"),
        "quoted_tweet": {"author": {"userName": r.get("quoted_username")}} if r.get("quoted_username") else None,
        "retweeted_tweet": None,
        "author": {
            "userName": r.get("author_username"),
            "followers": int(r.get("author_followers") or 0),
            "statusesCount": int(r.get("author_statuses") or 0),
            "createdAt": r.get("author_created_at"),
            "isAutomated": truthy(r.get("author_is_automated")),
            "isBlueVerified": truthy(r.get("author_verified")),
            "verifiedType": r.get("author_verified_type") or None,
        },
    }


# ---------------------------------------------------------------------------
# The pipeline, as a pure function: rows in → verdicts out
# ---------------------------------------------------------------------------
def apply_rules(rows: list[dict]) -> list[dict]:
    """Return one verdict per row: {tweet_id, drop_reason, source_type, outlet_handle}.
    Dedupe keeps the row that is currently clean (if any), else the earliest."""
    outlets = {h.lower() for h in C.OUTLETS}
    outlets |= {str(r["outlet_handle"]).lower() for r in rows if r.get("outlet_handle")}

    order = sorted(rows, key=lambda r: (r.get("drop_reason") is not None, r.get("created_at") or ""))
    seen_text: set[str] = set()
    verdicts = []
    for r in order:
        t = tweet_dict(r)
        thread_of = r.get("thread_of") or None
        reason = content_reason(t, outlets, thread_reply=(r.get("found_via") == "thread"))
        if reason is None:
            k = text_key(t)
            if k in seen_text:
                reason = "dup_text"
            else:
                seen_text.add(k)
        st, oh = classify(t, outlets, thread_of=thread_of)
        verdicts.append({"tweet_id": r["tweet_id"], "drop_reason": reason,
                         "source_type": st, "outlet_handle": oh})
    return verdicts


def funnel(rows: list[dict], verdicts: list[dict]) -> dict:
    by_reason = Counter(v["drop_reason"] or "kept" for v in verdicts)
    kept_ids = {v["tweet_id"] for v in verdicts if v["drop_reason"] is None}
    by_type = Counter(v["source_type"] for v in verdicts if v["drop_reason"] is None)
    by_lang = Counter((r.get("lang") or "?") for r in rows if r["tweet_id"] in kept_ids)
    steps, remaining = [], len(rows)
    steps.append({"step": "fetched (dc_tweets_raw)", "remaining": remaining})
    for reason in CONTENT_REASONS:
        n = by_reason.get(reason, 0)
        remaining -= n
        steps.append({"step": f"− {reason}", "dropped": n, "remaining": remaining})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_raw": len(rows),
        "n_clean": len(kept_ids),
        "funnel": steps,
        "dropped_by_reason": dict(by_reason),
        "clean_by_source_type": dict(by_type),
        "clean_by_lang": dict(by_lang),
        "source_types": C.SOURCE_TYPES,
        "rules": {
            "topic_regex": C.TOPIC_RE,
            "argentina_regex": C.ARGENTINA_RE,
            "argentina_false_regex": C.ARGENTINA_FALSE_RE,
            "long_text_chars": C.LONG_TEXT_CHARS,
            "proximity_chars": C.PROXIMITY_CHARS,
            "bot_rules": C.BOT_RULES,
            "verified_min_followers": C.VERIFIED_MIN_FOLLOWERS,
            "auto_outlet_min_followers": C.AUTO_OUTLET_MIN_FOLLOWERS,
            "auto_outlet_min_replies": C.AUTO_OUTLET_MIN_REPLIES,
        },
        "search_queries": C.SEARCH_QUERIES,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
CLEAN_COLS = ["tweet_id", "created_at", "text", "lang", "url", "author_id", "author_username",
              "author_name", "author_followers", "author_following", "author_statuses",
              "author_created_at", "author_verified", "author_verified_type", "author_is_automated",
              "author_description", "author_location", "like_count", "retweet_count", "reply_count",
              "quote_count", "view_count", "is_reply", "in_reply_to_id", "in_reply_to_username",
              "conversation_id", "quoted_tweet_id", "quoted_username", "source_type", "outlet_handle",
              "matched_query", "run_mode", "raw"]


def write_supabase(sb, rows: list[dict], verdicts: list[dict]) -> dict:
    by_id = {r["tweet_id"]: r for r in rows}
    changed_raw, clean_rows, now_dropped = [], [], []
    for v in verdicts:
        r = by_id[v["tweet_id"]]
        # "over_target" is not a content verdict: the row simply exceeded the scraper's per-run cap
        old = r.get("drop_reason") if r.get("drop_reason") != "over_target" else None
        if (old, r.get("source_type"), r.get("outlet_handle")) != (v["drop_reason"], v["source_type"], v["outlet_handle"]):
            changed_raw.append({**r, **v, "recleaned_at": datetime.now(timezone.utc).isoformat()})
        if v["drop_reason"] is None:
            clean_rows.append({**{k: r.get(k) for k in CLEAN_COLS}, **{k: v[k] for k in ("source_type", "outlet_handle")}})
        elif r.get("drop_reason") is None:           # was in dc_tweets, no longer passes
            now_dropped.append(v["tweet_id"])

    for i in range(0, len(changed_raw), 200):
        sb.table(C.SUPABASE_RAW_TABLE).upsert(changed_raw[i:i + 200], on_conflict="tweet_id").execute()
    for i in range(0, len(clean_rows), 200):
        sb.table(C.SUPABASE_TABLE).upsert(clean_rows[i:i + 200], on_conflict="tweet_id").execute()
    for i in range(0, len(now_dropped), 200):
        sb.table(C.SUPABASE_TABLE).delete().in_("tweet_id", now_dropped[i:i + 200]).execute()
    return {"raw_updated": len(changed_raw), "clean_upserted": len(clean_rows), "clean_deleted": len(now_dropped)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="read a CSV export of dc_tweets_raw (no Supabase)")
    ap.add_argument("--dry-run", action="store_true", help="compute, print, write nothing to Supabase")
    ap.add_argument("--summary", default="out/pipeline_summary.json")
    ap.add_argument("--out-clean", default=None, help="also write the clean rows to this CSV")
    args = ap.parse_args()

    sb = None
    if args.csv:
        rows = load_raw_csv(args.csv)
    else:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
        rows = load_raw_supabase(sb)
    print(f"{len(rows)} rows in {C.SUPABASE_RAW_TABLE}")

    verdicts = apply_rules(rows)
    summary = funnel(rows, verdicts)
    for s in summary["funnel"]:
        print(f"  {s['step']:<28} {s.get('dropped', ''):>6}  → {s['remaining']}")
    print(f"clean by segment: {summary['clean_by_source_type']}")

    stored = Counter((r.get("drop_reason") or "kept") if r.get("drop_reason") != "over_target" else "kept" for r in rows)
    diff = {k: summary["dropped_by_reason"].get(k, 0) - stored.get(k, 0)
            for k in set(stored) | set(summary["dropped_by_reason"]) if summary["dropped_by_reason"].get(k, 0) != stored.get(k, 0)}
    print(f"change vs. stored verdicts: {diff or 'none'}")

    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.summary}")

    if args.out_clean:
        by_id = {r["tweet_id"]: r for r in rows}
        with open(args.out_clean, "w", newline="", encoding="utf-8") as f:
            cols = [c for c in CLEAN_COLS if c != "raw"]
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for v in verdicts:
                if v["drop_reason"] is None:
                    w.writerow({**by_id[v["tweet_id"]], **v})
        print(f"wrote {args.out_clean}")

    if sb is not None and not args.dry_run:
        print(write_supabase(sb, rows, verdicts))


if __name__ == "__main__":
    main()
