#!/usr/bin/env python3
"""Collection gates and the dc_tweets mirror. No API, no Supabase.

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
import gates  # noqa: E402


def _row(tid, drop_reason, text="El data center en la Patagonia", **raw_extra):
    raw = {
        "id": tid,
        "text": text,
        "author": {"userName": "someone", "followers": 10},
        "isReply": False,
    }
    raw.update(raw_extra)
    return {
        "tweet_id": tid,
        "drop_reason": drop_reason,
        "text": text,
        "outlet_handle": None,
        "thread_of": None,
        "found_via": "search",
        "raw": raw,
    }


class CollectionTests(unittest.TestCase):
    def test_only_a_retweet_is_dropped(self):
        kept = {"id": "1", "text": "hola", "retweeted_tweet": None}
        rt = {"id": "2", "text": "RT @x", "retweeted_tweet": {"id": "9"}}
        self.assertIsNone(gates.collection_reason(kept))
        self.assertEqual(gates.collection_reason(rt), "retweet")

    def test_thread_gate_requires_both_terms_and_ignores_false_phrases(self):
        self.assertTrue(gates.is_on_topic("Nuevo data center en la Patagonia"))
        self.assertFalse(gates.is_on_topic("the weather in Paris is lovely today"))
        # "than Argentina" is not the place, so this does not open a thread.
        self.assertFalse(gates.is_on_topic("This data center is bigger than Argentina"))

    def test_classify_still_labels_an_outlet_post(self):
        tweet = {"text": "x", "isReply": False, "author": {"userName": "clarincom", "followers": 10}}
        self.assertEqual(gates.classify(tweet, {"clarincom"}), ("outlet_post", "clarincom"))

    def test_daily_knobs_are_unchanged(self):
        self.assertEqual(C.DAILY["lookback_hours"], 48)
        self.assertEqual(C.DAILY["fresh_pages_per_query"], 2)
        self.assertEqual(C.DAILY["max_pages_per_query_window"], 4)
        self.assertEqual(C.DAILY["target_new_tweets"], 1000)
        self.assertEqual(C.DAILY["max_threads"], 40)
        self.assertEqual(C.DAILY["max_thread_pages"], 3)
        self.assertEqual(C.DAILY["hard_cap_tweets_fetched"], 2500)
        self.assertEqual(C.BUDGET_USD, 18.0)
        self.assertTrue(C.DAILY.get("start_date"))
        self.assertFalse(hasattr(C, "BOT_RULES"))

    def test_analysis_helpers_are_gone(self):
        self.assertFalse((ROOT / "filters.py").exists())
        for name in ("content_reason", "is_bot", "text_key", "visible_text", "apply_rules"):
            self.assertFalse(hasattr(gates, name), name)
            self.assertFalse(hasattr(clean, name), name)

    def test_mirror_keeps_null_and_over_target_and_leaves_old_labels(self):
        rows = [
            _row("kept", None),
            _row("cap", "over_target"),
            _row("old", "bot"),
            _row("short", "short"),
            _row("off", "offtopic"),
            _row("dup", "dup_text"),
            _row("rt", "retweet", retweeted_tweet={"id": "9"}),
        ]
        verdicts = {v["tweet_id"]: v["drop_reason"] for v in clean.mirror_verdicts(rows)}
        self.assertIsNone(verdicts["kept"])
        self.assertIsNone(verdicts["cap"])          # over_target is copied into dc_tweets
        self.assertEqual(verdicts["old"], "bot")
        self.assertEqual(verdicts["short"], "short")
        self.assertEqual(verdicts["off"], "offtopic")
        self.assertEqual(verdicts["dup"], "dup_text")
        self.assertEqual(verdicts["rt"], "retweet")


if __name__ == "__main__":
    unittest.main()
