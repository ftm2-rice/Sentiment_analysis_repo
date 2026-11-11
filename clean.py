#!/usr/bin/env python3
"""
clean.py — mirror scraper-kept rows from dc_tweets_raw into dc_tweets.

    python clean.py                     # Supabase → Supabase; writes out/pipeline_summary.json
    python clean.py --dry-run           # print the mirror count, write nothing
    python clean.py --csv raw.csv       # read a CSV export of dc_tweets_raw; never writes to Supabase

scrape.py already writes both tables for the rows it just fetched. This script
re-reads the whole raw table and makes dc_tweets match: NULL and over_target
rows are in, retweets are out. source_type is refreshed with gates.classify().

Rows collected by an older scraper may still say bot, short, offtopic, or
dup_text. Those labels are not assigned here. The mirror leaves them as stored:
it does not copy them into dc_tweets, and it does not delete them.

Analysis filtering (bot, short, offtopic, dup_text) lives in the Colab notebook,
not in this file.

Env vars (Supabase mode): SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse                             # command-line flags (--csv, --dry-run…)
import csv                                  # read/write CSV files
import json                                 # the `raw` column is JSON text in a CSV
import os                                   # environment variables
import sys                                  # to add the repo folder to the import path
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))      # so `import config` works from any folder
import config as C  # noqa: E402
from gates import classify  # noqa: E402

# Older scrapes wrote these drop_reason values. This repo no longer assigns them.
# The mirror does not promote those rows and does not delete them.
LEFT_AS_STORED = ("bot", "short", "offtopic", "dup_text")


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
# Mirror: which raw rows belong in dc_tweets
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
    over_target label is left alone when the other fields match). Rows whose
    drop_reason is in LEFT_AS_STORED are returned unchanged.
    source_type is refreshed with classify().
    """
    outlets = {h.lower() for h in C.OUTLETS}
    outlets |= {str(r["outlet_handle"]).lower() for r in rows if r.get("outlet_handle")}
    verdicts = []
    for r in rows:
        t = tweet_dict(r)
        stored = r.get("drop_reason") or None
        if t.get("retweeted_tweet") or stored == "retweet":
            reason = "retweet"
        elif stored in LEFT_AS_STORED:
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
        # LEFT_AS_STORED (old bot/short/offtopic/dup_text labels): do not delete.

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

    # 2. what dc_tweets should hold. No analysis funnel — that lives in Colab.
    verdicts = mirror_verdicts(rows)
    n_mirror = sum(1 for v in verdicts if v["drop_reason"] is None)
    print(f"dc_tweets mirror: {n_mirror} scraper-kept rows")
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pipeline": "GitHub Actions collects; Colab filters",
        "n_raw": len(rows),
        "n_mirror": n_mirror,
        "note": (
            "dc_tweets mirrors scraper-kept rows (drop_reason null or over_target, "
            "retweets excluded). bot/short/offtopic/dup_text are applied in Colab, "
            "not here. Older rows that already carry those labels are left as stored."
        ),
    }

    # 3. write the summary JSON
    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.summary}")

    # 4. optional CSV of the mirror rows
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

    # 5. write the mirror back (unless --csv or --dry-run)
    if sb is not None and not args.dry_run:
        print(write_supabase(sb, rows, verdicts))


if __name__ == "__main__":
    main()
