#!/usr/bin/env python3
"""
clean.py — keep dc_tweets in step with dc_tweets_raw.

    python clean.py                     # mirror scraper-kept rows into dc_tweets
    python clean.py --dry-run           # print the counts, write nothing to Supabase
    python clean.py --csv raw.csv       # read a CSV export instead of Supabase; never writes
    python clean.py --promote-historical
                                        # ALSO copy old bot/short/offtopic/dup_text rows
                                        # into dc_tweets and clear those drop_reasons.
                                        # Off by default: the Streamlit app reads dc_tweets,
                                        # and promoting history would change its next export.

The scraper collects (cost-saving gates only) and the Colab notebook filters.
This script does not drop bots, short texts, off-topic posts or duplicate texts.
dc_tweets mirrors the rows the scraper kept: drop_reason NULL or over_target,
excluding retweets. Historical analysis reasons are left as stored.

`apply_rules` / `funnel` are still here so notebooks/pipeline.ipynb can show the
Colab funnel. The daily job does not call them.

Nothing in this file deletes rows.

Env vars (Supabase mode): SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse                             # command-line flags (--csv, --dry-run…)
import csv                                  # read/write CSV files
import json                                 # the `raw` column is JSON text in a CSV
import os                                   # environment variables
import sys                                  # to add the repo folder to the import path
from collections import Counter             # counting verdicts
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))      # so `import config` works from any folder
import config as C  # noqa: E402
from filters import (CONTENT_REASONS, ANALYSIS_REASONS, content_reason,  # noqa: E402
                     classify, text_key)


# ---------------------------------------------------------------------------
# Input: get the raw rows, from Supabase or from a CSV
# ---------------------------------------------------------------------------
def load_raw_supabase(sb) -> list[dict]:
    """Read the whole raw table, 1,000 rows at a time (Supabase caps a single select)."""
    rows, start, step = [], 0, 1000
    while True:
        res = sb.table(C.SUPABASE_RAW_TABLE).select("*").range(start, start + step - 1).execute()
        data = res.data or []                               # this page of rows
        rows.extend(data)
        if len(data) < step:                                # a short page means it was the last one
            return rows
        start += step                                       # next page


def load_raw_csv(path: str) -> list[dict]:
    """Read a CSV export of the raw table. Empty strings become None, as in the database."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))                      # one dict per row, keyed by column name
    for r in rows:
        for k in ("drop_reason", "thread_of", "outlet_handle", "source_type", "found_via"):
            if r.get(k) == "":
                r[k] = None                                 # CSV has no NULL, only ""
    return rows


def tweet_dict(r: dict) -> dict:
    """The API-shaped object the rules expect. Prefer the stored `raw` JSON; if it is
    missing, rebuild an approximation from the flat columns (loses the quoted tweet's text)."""
    raw = r.get("raw")
    if isinstance(raw, str) and raw.strip():                # from a CSV the JSON is a string
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict) and raw.get("text") is not None:
        return raw                                          # the real thing
    truthy = lambda v: str(v).lower() in ("true", "1", "t")  # noqa: E731  CSV booleans are text
    return {                                                # fallback: minimal object from the columns
        "id": r["tweet_id"],
        "text": r.get("text") or "",
        "createdAt": r.get("created_at"),
        "isReply": truthy(r.get("is_reply")),
        "inReplyToUsername": r.get("in_reply_to_username"),
        "quoted_tweet": {"author": {"userName": r.get("quoted_username")}} if r.get("quoted_username") else None,
        "retweeted_tweet": None,                            # not stored as a column; rely on drop_reason
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
# The pipeline as a pure function: rows in → one verdict per row out
# ---------------------------------------------------------------------------
def apply_rules(rows: list[dict]) -> list[dict]:
    """Colab-side verdict per row: {tweet_id, drop_reason, source_type, outlet_handle}.

    Inspection only — the daily job does not write these verdicts back.
    Dedupe keeps the row that is currently clean (if any), else the earliest one.
    """
    outlets = {h.lower() for h in C.OUTLETS}                                            # the configured outlets…
    outlets |= {str(r["outlet_handle"]).lower() for r in rows if r.get("outlet_handle")}  # …plus auto-discovered ones

    # Judge the currently-clean rows first, then the rest by date, so that when two
    # rows share the same text the one already in dc_tweets is the one that survives.
    order = sorted(rows, key=lambda r: (r.get("drop_reason") is not None, r.get("created_at") or ""))
    seen_text: set[str] = set()                             # normalised texts already kept
    verdicts = []
    for r in order:
        t = tweet_dict(r)                                   # API-shaped object
        thread_of = r.get("thread_of") or None              # outlet whose thread it came from, if any
        reason = content_reason(t, outlets, thread_reply=(r.get("found_via") == "thread"))   # rules 1–2
        if reason is None:                                  # passed the content rules → check duplicates
            k = text_key(t)
            if k in seen_text:
                reason = "dup_text"
            else:
                seen_text.add(k)
        st, oh = classify(t, outlets, thread_of=thread_of)  # rule 3: segment
        verdicts.append({"tweet_id": r["tweet_id"], "drop_reason": reason,
                         "source_type": st, "outlet_handle": oh})
    return verdicts


def funnel(rows: list[dict], verdicts: list[dict]) -> dict:
    """The Colab analysis funnel (bot / short / offtopic / dup_text), for the notebook.

    This is not what scrape.py drops and not what the daily clean.py writes.
    """
    by_reason = Counter(v["drop_reason"] or "kept" for v in verdicts)                   # how many per reason
    kept_ids = {v["tweet_id"] for v in verdicts if v["drop_reason"] is None}            # ids that passed
    by_type = Counter(v["source_type"] for v in verdicts if v["drop_reason"] is None)   # segments among kept
    by_lang = Counter((r.get("lang") or "?") for r in rows if r["tweet_id"] in kept_ids)  # languages among kept
    steps, remaining = [], len(rows)
    steps.append({"step": "fetched (dc_tweets_raw)", "remaining": remaining})
    for reason in CONTENT_REASONS:                          # subtract each rule in pipeline order
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
        "source_types": C.SOURCE_TYPES,                     # segment names + meaning (for the app's glossary)
        "rules": {                                          # the thresholds in force when this was generated
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
# The columns of dc_tweets (the raw table has these plus drop_reason, found_via, thread_of, run_id, legacy).
CLEAN_COLS = ["tweet_id", "created_at", "text", "lang", "url", "author_id", "author_username",
              "author_name", "author_followers", "author_following", "author_statuses",
              "author_created_at", "author_verified", "author_verified_type", "author_is_automated",
              "author_description", "author_location", "like_count", "retweet_count", "reply_count",
              "quote_count", "view_count", "is_reply", "in_reply_to_id", "in_reply_to_username",
              "conversation_id", "quoted_tweet_id", "quoted_username", "source_type", "outlet_handle",
              "matched_query", "run_mode", "raw"]


def clean_table_state(sb) -> dict[str, tuple]:
    """What dc_tweets holds right now: tweet_id → (source_type, outlet_handle). Read in pages."""
    out, start, step = {}, 0, 1000
    while True:
        res = sb.table(C.SUPABASE_TABLE).select("tweet_id,source_type,outlet_handle") \
                .range(start, start + step - 1).execute()
        data = res.data or []
        for r in data:
            out[r["tweet_id"]] = (r.get("source_type"), r.get("outlet_handle"))
        if len(data) < step:
            return out
        start += step


def _batched(sb, table: str, rows: list[dict], size: int = 50):
    """Upsert in small batches (rows carry the JSON `raw` blob, so big batches break the connection)."""
    for i in range(0, len(rows), size):
        sb.table(table).upsert(rows[i:i + size], on_conflict="tweet_id").execute()


def mirror_keep(row: dict, promote_historical: bool = False) -> bool:
    """True if this raw row belongs in dc_tweets.

    Scraper-kept rows (drop_reason NULL or over_target) are in. Retweets are out.
    Historical analysis reasons (bot, short, offtopic, dup_text) stay out unless
    promote_historical is set — promoting them is a choice for the owner, because
    the Streamlit app reads dc_tweets.
    """
    reason = row.get("drop_reason") or None
    if reason == "retweet":
        return False
    if tweet_dict(row).get("retweeted_tweet"):
        return False                                        # raw JSON says it's a retweet
    if reason in ANALYSIS_REASONS:
        return promote_historical
    return True                                             # NULL, over_target, or a reason we don't know


def collection_summary(rows: list[dict], promote_historical: bool = False) -> dict:
    """What the daily job actually did: counts by the drop_reason stored on the row.

    Not the Colab funnel. `n_mirror` is how many rows belong in dc_tweets.
    """
    by_reason = Counter((r.get("drop_reason") or "kept") for r in rows)
    kept = [r for r in rows if mirror_keep(r, promote_historical)]
    outlets = {h.lower() for h in C.OUTLETS}
    outlets |= {str(r["outlet_handle"]).lower() for r in rows if r.get("outlet_handle")}
    by_type = Counter()
    for r in kept:
        st, _oh = classify(tweet_dict(r), outlets, thread_of=r.get("thread_of") or None)
        by_type[st] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pipeline": "scraper collects (cost-saving gates only); Colab filters",
        "n_raw": len(rows),
        "n_mirror": len(kept),                              # rows that belong in dc_tweets
        "promote_historical": promote_historical,
        "stored_drop_reasons": dict(by_reason),
        "mirror_by_source_type": dict(by_type),
        "source_types": C.SOURCE_TYPES,
        "note": (
            "dc_tweets mirrors scraper-kept rows (drop_reason null or over_target, "
            "retweets excluded). bot/short/offtopic/dup_text are applied in the Colab "
            "notebook that reads dc_tweets_raw, not here. Historical rows that already "
            "carry those reasons are not copied into dc_tweets unless "
            "--promote-historical is passed. This script does not delete rows."
        ),
    }


def sync_mirror(sb, rows: list[dict], promote_historical: bool = False) -> dict:
    """Upsert scraper-kept rows into dc_tweets and refresh source_type labels.

    Does not delete anything. With promote_historical, old analysis rejects are
    copied in and their drop_reason is cleared (the Colab recomputes it).
    """
    outlets = {h.lower() for h in C.OUTLETS}
    outlets |= {str(r["outlet_handle"]).lower() for r in rows if r.get("outlet_handle")}
    in_clean = clean_table_state(sb)
    changed_raw, to_upsert = [], []
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if not mirror_keep(r, promote_historical):
            continue
        t = tweet_dict(r)
        st, oh = classify(t, outlets, thread_of=r.get("thread_of") or None)
        reason = r.get("drop_reason") or None
        clear_reason = promote_historical and reason in ANALYSIS_REASONS
        labels_differ = (r.get("source_type"), r.get("outlet_handle")) != (st, oh)
        if clear_reason or labels_differ:
            updated = {**r, "source_type": st, "outlet_handle": oh, "recleaned_at": now}
            if clear_reason:
                updated["drop_reason"] = None              # Colab will judge this row itself
            changed_raw.append(updated)
        if in_clean.get(r["tweet_id"]) != (st, oh):
            to_upsert.append({**{k: r.get(k) for k in CLEAN_COLS},
                              "source_type": st, "outlet_handle": oh})
    _batched(sb, C.SUPABASE_RAW_TABLE, changed_raw)
    _batched(sb, C.SUPABASE_TABLE, to_upsert)
    return {"raw_updated": len(changed_raw), "clean_upserted": len(to_upsert), "clean_deleted": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="read a CSV export of dc_tweets_raw (no Supabase)")
    ap.add_argument("--dry-run", action="store_true", help="compute, print, write nothing to Supabase")
    ap.add_argument("--summary", default="out/pipeline_summary.json")
    ap.add_argument("--out-clean", default=None, help="also write the mirror rows to this CSV")
    ap.add_argument("--promote-historical", action="store_true",
                    help="also copy historical bot/short/offtopic/dup_text rows into dc_tweets "
                         "and clear those drop_reasons. Changes what a future Streamlit export sees.")
    args = ap.parse_args()

    # 1. load
    sb = None
    if args.csv:
        rows = load_raw_csv(args.csv)
    else:
        from supabase import create_client                  # imported here so CSV mode needs no supabase package
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
        rows = load_raw_supabase(sb)
    print(f"{len(rows)} rows in {C.SUPABASE_RAW_TABLE}")
    print("analysis filters (bot, short, offtopic, dup_text) are NOT applied here — they live in the Colab notebook")

    # 2. mirror counts (what dc_tweets should hold), not the Colab funnel
    summary = collection_summary(rows, promote_historical=args.promote_historical)
    print(f"stored drop_reason: {summary['stored_drop_reasons']}")
    print(f"mirror (dc_tweets): {summary['n_mirror']} | by segment: {summary['mirror_by_source_type']}")
    if args.promote_historical:
        print("promote-historical is ON — old analysis rejects will be copied into dc_tweets")

    # 3. write the summary JSON
    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.summary}")

    # 4. optional CSV of the mirror rows
    if args.out_clean:
        outlets = {h.lower() for h in C.OUTLETS}
        outlets |= {str(r["outlet_handle"]).lower() for r in rows if r.get("outlet_handle")}
        with open(args.out_clean, "w", newline="", encoding="utf-8") as f:
            cols = [c for c in CLEAN_COLS if c != "raw"]    # the JSON column is too big for a CSV
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                if mirror_keep(r, args.promote_historical):
                    st, oh = classify(tweet_dict(r), outlets, thread_of=r.get("thread_of") or None)
                    w.writerow({**r, "source_type": st, "outlet_handle": oh})
        print(f"wrote {args.out_clean}")

    # 5. write back to Supabase (unless --csv or --dry-run). Never deletes.
    if sb is not None and not args.dry_run:
        print(sync_mirror(sb, rows, promote_historical=args.promote_historical))


if __name__ == "__main__":
    main()
