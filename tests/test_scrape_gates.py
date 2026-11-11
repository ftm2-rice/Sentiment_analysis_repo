#!/usr/bin/env python3
"""Checks that the scraper no longer applies Colab filters. No API, no Supabase.

    python tests/test_scrape_gates.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import clean  # noqa: E402
import config as C  # noqa: E402
import filters as F  # noqa: E402


def _tweet(tid, text="El data center en la Patagonia necesita energía", **extra):
    t = {
        "id": tid,
        "text": text,
        "author": {"userName": "someone", "followers": 10, "isAutomated": False},
        "isReply": False,
        "replyCount": 0,
    }
    t.update(extra)
    return t


class FilterSplitTests(unittest.TestCase):
    def test_collection_keeps_bot_short_offtopic_and_duplicate_text(self):
        bot = _tweet("b", author={"userName": "noticias24_bot", "isAutomated": True, "followers": 10,
                                  "statusesCount": 100, "createdAt": "Mon Jan 02 10:00:00 +0000 2020"})
        short = _tweet("s", text="ok")
        offtopic = _tweet("o", text="the weather in Paris is lovely today, bring a coat")
        dup_a = _tweet("d1", text="El data center en la Patagonia necesita energía de verdad")
        dup_b = _tweet("d2", text=dup_a["text"])
        for t in (bot, short, offtopic, dup_a, dup_b):
            self.assertIsNone(F.collection_reason(t))
        # Colab rules still fire. The scraper simply does not call them.
        self.assertEqual(F.content_reason(bot, set()), "bot")
        self.assertEqual(F.content_reason(short, set()), "short")
        self.assertEqual(F.content_reason(offtopic, set()), "offtopic")
        self.assertEqual(F.text_key(dup_a), F.text_key(dup_b))

    def test_retweet_is_still_the_collection_drop(self):
        rt = _tweet("r", retweeted_tweet={"id": "other"})
        self.assertEqual(F.collection_reason(rt), "retweet")
        self.assertEqual(F.content_reason(rt, set()), "retweet")

    def test_daily_collection_knobs_are_unchanged(self):
        # Cost/window behavior stays on the original daily settings.
        self.assertEqual(C.DAILY["lookback_hours"], 48)
        self.assertEqual(C.DAILY["fresh_pages_per_query"], 2)
        self.assertEqual(C.DAILY["max_pages_per_query_window"], 4)
        self.assertEqual(C.DAILY["target_new_tweets"], 1000)
        self.assertEqual(C.DAILY["max_threads"], 40)
        self.assertNotIn("backfill_history", C.DAILY)
        self.assertNotIn("fresh_overlap_hours", C.DAILY)
        self.assertTrue(C.DAILY.get("start_date"))

    def test_sample_analysis_still_flags_and_mirror_does_not_promote(self):
        rows = clean.load_raw_csv(str(ROOT / "notebooks" / "sample_raw.csv"))
        analysis = {v["tweet_id"]: v["drop_reason"] for v in clean.apply_rules(rows)}
        for reason in ("retweet", "bot", "short", "offtopic", "dup_text"):
            self.assertIn(reason, analysis.values(), reason)
        mirror = {v["tweet_id"]: v["drop_reason"] for v in clean.mirror_verdicts(rows)}
        for r in rows:
            stored = r.get("drop_reason") or None
            got = mirror[r["tweet_id"]]
            if stored in ("bot", "short", "offtopic", "dup_text", "retweet"):
                self.assertEqual(got, stored)
            else:
                self.assertIsNone(got)          # NULL (and over_target) count as kept


if __name__ == "__main__":
    unittest.main()
