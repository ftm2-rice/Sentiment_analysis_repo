#!/usr/bin/env python3
"""
clean.py — keep dc_tweets in step with what the scraper kept.

    python clean.py                     # Supabase → Supabase; writes out/pipeline_summary.json
    python clean.py --dry-run           # print counts, write nothing to Supabase
    python clean.py --csv raw.csv       # read a CSV export of dc_tweets_raw instead of Supabase
                                        # (what the notebook does); never writes to Supabase

The scraper collects (retweets and known ids only). The Colab notebook filters
(bot, short, offtopic, dup_text) when it reads dc_tweets_raw.

This script does not re-apply those Colab filters. dc_tweets mirrors scraper-kept
rows: drop_reason NULL or over_target, retweets excluded. Historical rows that
already say bot/short/offtopic/dup_text are left as stored — not promoted into
dc_tweets, and not deleted if they are already there. Nothing else is deleted
except a retweet that is still sitting in dc_tweets.

apply_rules / funnel still reproduce the Colab funnel for the notebook. The
daily job prints that funnel and does not write it back.

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
    """Colab inspection verdict per row. The daily job does not write this back.

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
    """Colab analysis funnel for the notebook. Not what the daily job writes to dc_tweets."""
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


def mirror_verdicts(rows: list[dict]) -> list[dict]:
    """One verdict per row for the dc_tweets mirror. Does not apply Colab filters.

    Retweets stay out. NULL and over_target count as kept (over_target is reported
    as None here so write_supabase copies the row into dc_tweets; the stored
    over_target label is left alone when the other fields match). Historical
    bot/short/offtopic/dup_text reasons are returned unchanged.
    source_type is still refreshed with classify().
    """
    outlets = {h.lower() for h in C.OUTLETS}
    outlets |= {str(r["outlet_handle"]).lower() for r in rows if r.get("outlet_handle")}
    verdicts = []
    for r in rows:
        t = tweet_dict(r)
        stored = r.get("drop_reason") or None
        if t.get("retweeted_tweet") or stored == "retweet":
            reason = "retweet"
        elif stored in ANALYSIS_REASONS:
            reason = stored                               # do not promote, do not clear
        else:
            reason = None                                 # NULL or over_target → in dc_tweets
        st, oh = classify(t, outlets, thread_of=r.get("thread_of") or None)
        verdicts.append({"tweet_id": r["tweet_id"], "drop_reason": reason,
                         "source_type": st, "outlet_handle": oh})
    return verdicts


def write_supabase(sb, rows: list[dict], verdicts: list[dict]) -> dict:
    """Push the verdicts back. Compares against what the tables hold NOW, so it is safe to
    re-run after an interrupted attempt: only rows that differ are written."""
    by_id = {r["tweet_id"]: r for r in rows}                # quick lookup of the raw row by id
    in_clean = clean_table_state(sb)                        # what dc_tweets currently contains
    changed_raw, to_upsert, to_delete = [], [], []
    for v in verdicts:
        r = by_id[v["tweet_id"]]
        # "over_target" is not a content verdict (the row just exceeded the scraper's per-run cap),
        # so for comparison purposes treat it as "kept".
        old = r.get("drop_reason") if r.get("drop_reason") != "over_target" else None
        if (old, r.get("source_type"), r.get("outlet_handle")) != (v["drop_reason"], v["source_type"], v["outlet_handle"]):
            changed_raw.append({**r, **v, "recleaned_at": datetime.now(timezone.utc).isoformat()})   # raw verdict changed
        if v["drop_reason"] is None:                        # should be in dc_tweets…
            if in_clean.get(v["tweet_id"]) != (v["source_type"], v["outlet_handle"]):   # …and isn't, or differs
                to_upsert.append({**{k: r.get(k) for k in CLEAN_COLS},
                                  **{k: v[k] for k in ("source_type", "outlet_handle")}})
        elif v["drop_reason"] == "retweet" and v["tweet_id"] in in_clean:
            to_delete.append(v["tweet_id"])                 # retweets stay out of dc_tweets
        # bot/short/offtopic/dup_text: leave the row where it is. Do not delete it.

    _batched(sb, C.SUPABASE_RAW_TABLE, changed_raw)
    _batched(sb, C.SUPABASE_TABLE, to_upsert)
    for i in range(0, len(to_delete), 200):
        sb.table(C.SUPABASE_TABLE).delete().in_("tweet_id", to_delete[i:i + 200]).execute()
    return {"raw_updated": len(changed_raw), "clean_upserted": len(to_upsert), "clean_deleted": len(to_delete)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="read a CSV export of dc_tweets_raw (no Supabase)")
    ap.add_argument("--dry-run", action="store_true", help="compute, print, write nothing to Supabase")
    ap.add_argument("--summary", default="out/pipeline_summary.json")
    ap.add_argument("--out-clean", default=None, help="also write the clean rows to this CSV")
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

    # 2. Colab funnel is printed for inspection only. It is not written back.
    inspection = apply_rules(rows)
    summary = funnel(rows, inspection)
    summary["pipeline"] = "scraper collects (cost-saving gates only); Colab filters"
    summary["note"] = (
        "The funnel above is the Colab inspection (apply_rules). It is not written to "
        "dc_tweets. The mirror is scraper-kept rows: drop_reason null or over_target, "
        "retweets excluded. Historical bot/short/offtopic/dup_text rows are left as stored."
    )
    print("Colab inspection only (not written to dc_tweets):")
    for s in summary["funnel"]:
        print(f"  {s['step']:<28} {s.get('dropped', ''):>6}  → {s['remaining']}")

    # 3. what dc_tweets should hold
    verdicts = mirror_verdicts(rows)
    summary["n_mirror"] = sum(1 for v in verdicts if v["drop_reason"] is None)
    print(f"dc_tweets mirror: {summary['n_mirror']} scraper-kept rows")

    # 4. write the summary JSON (the notebook and the Streamlit app read this)
    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.summary}")

    # 5. optional CSV of the clean rows
    if args.out_clean:
        by_id = {r["tweet_id"]: r for r in rows}
        with open(args.out_clean, "w", newline="", encoding="utf-8") as f:
            cols = [c for c in CLEAN_COLS if c != "raw"]    # the JSON column is too big for a CSV
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for v in verdicts:
                if v["drop_reason"] is None:
                    w.writerow({**by_id[v["tweet_id"]], **v})
        print(f"wrote {args.out_clean}")

    # 6. write the mirror back (unless --csv or --dry-run). Does not re-apply Colab filters.
    if sb is not None and not args.dry_run:
        print(write_supabase(sb, rows, verdicts))


if __name__ == "__main__":
    main()
