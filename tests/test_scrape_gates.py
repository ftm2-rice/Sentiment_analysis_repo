#!/usr/bin/env python3
"""Unit checks for the scraper's cost gates. No twitterapi.io calls, no Supabase.

    python tests/test_scrape_gates.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import clean  # noqa: E402
import filters as F  # noqa: E402
import scrape  # noqa: E402


NOW = datetime(2026, 11, 11, 12, 10, tzinfo=timezone.utc)   # a Wednesday, like the cron


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


class Resp:
    def __init__(self, status, payload, text=""):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Hands back scripted responses. `get` raises if the script says to."""

    def __init__(self, script):
        self.headers = {}
        self.calls = 0
        self.script = list(script)

    def get(self, url, params=None, timeout=None):
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class FreshWindowTests(unittest.TestCase):
    def test_no_watermark_uses_lookback(self):
        start = scrape.fresh_since(NOW, 26, 2, 24 * 14, None)
        self.assertEqual(start, NOW - timedelta(hours=26))

    def test_recent_watermark_is_tighter_than_lookback(self):
        wm = NOW - timedelta(hours=3)
        start = scrape.fresh_since(NOW, 26, 2, 24 * 14, wm)
        self.assertEqual(start, wm - timedelta(hours=2))          # 5 h, not 26 h
        self.assertGreater(start, NOW - timedelta(hours=26))

    def test_old_watermark_is_capped(self):
        wm = NOW - timedelta(days=30)
        start = scrape.fresh_since(NOW, 26, 2, 24 * 14, wm)
        self.assertEqual(start, NOW - timedelta(hours=24 * 14))

    def test_finished_run_claims_now_and_a_hole_does_not(self):
        prev = NOW - timedelta(hours=26)
        self.assertEqual(scrape.claim_watermark(NOW, prev, aborted=False, over_cap=False, redo=[]), NOW)
        self.assertEqual(scrape.claim_watermark(NOW, prev, aborted=True, over_cap=False, redo=[]), prev)
        self.assertEqual(scrape.claim_watermark(NOW, prev, aborted=False, over_cap=False, redo=[3]), prev)
        self.assertEqual(scrape.claim_watermark(NOW, prev, aborted=False, over_cap=True, redo=[]), prev)

    def test_daily_hist_is_only_the_current_week(self):
        fresh, hist = scrape.time_windows("daily", now=NOW, watermark=None)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh[1] - fresh[0], 26 * 3600)
        monday = datetime(2026, 11, 9, tzinfo=timezone.utc)       # Monday of that week
        self.assertEqual(hist, [(int(monday.timestamp()), int(NOW.timestamp()))])

    def test_daily_window_shrinks_to_the_watermark(self):
        wm = NOW - timedelta(hours=4)
        fresh, _hist = scrape.time_windows("daily", now=NOW, watermark=wm)
        self.assertEqual(fresh[0], int((wm - timedelta(hours=2)).timestamp()))

    def test_backfill_still_walks_history(self):
        fresh, hist = scrape.time_windows("backfill", now=NOW)
        self.assertIsNone(fresh)
        self.assertGreater(len(hist), 10)
        # Oldest cell starts at HISTORY_START (2024-12-01), not before it.
        floor = datetime(2024, 12, 1, tzinfo=timezone.utc)
        self.assertEqual(hist[-1][0], int(floor.timestamp()))
        self.assertLess(hist[-1][0], hist[0][0])


class PaginationTests(unittest.TestCase):
    def test_page_all_known_requires_every_id(self):
        already = {"1", "2"}
        self.assertTrue(scrape.page_all_known([{"id": "1"}, {"id": "2"}], already))
        self.assertFalse(scrape.page_all_known([{"id": "1"}, {"id": "3"}], already))
        self.assertFalse(scrape.page_all_known([{"id": "1"}, {}], already))   # missing id: don't guess
        self.assertFalse(scrape.page_all_known([], already))

    def test_stop_is_decided_before_the_page_is_added(self):
        """A page of new tweets must not look 'known' just because we then store it."""
        pages = [[{"id": "1"}, {"id": "2"}], [{"id": "3"}, {"id": "4"}], [{"id": "5"}]]
        already = set()
        fetched_pages = []
        for page in pages:
            stop = scrape.page_all_known(page, already)          # BEFORE adding
            fetched_pages.append(page)
            for t in page:
                already.add(str(t["id"]))
            if stop:
                break
        self.assertEqual(len(fetched_pages), 3)                  # nothing was known, so we read all

        already = {"1", "2"}
        fetched_pages = []
        for page in pages:
            stop = scrape.page_all_known(page, already)
            fetched_pages.append(page)
            for t in page:
                already.add(str(t["id"]))
            if stop:
                break
        self.assertEqual(len(fetched_pages), 1)                  # page 1 was entirely known

    def test_client_does_not_request_the_next_page(self):
        session = FakeSession([
            Resp(200, {"tweets": [{"id": "1"}, {"id": "2"}], "has_next_page": True, "next_cursor": "c"}),
            Resp(200, {"tweets": [{"id": "3"}], "has_next_page": False}),
        ])
        client = scrape.Client("test-key", 1000, session=session, sleep=lambda _s: None)
        got = list(client.search("q", stop_if_page=lambda tw: scrape.page_all_known(tw, {"1", "2"})))
        self.assertEqual([t["id"] for t in got], ["1", "2"])
        self.assertEqual(session.calls, 1)
        self.assertTrue(client.last_page_stop)
        self.assertEqual(client.tweets_fetched, 2)

    def test_client_keeps_going_when_the_page_has_a_new_id(self):
        session = FakeSession([
            Resp(200, {"tweets": [{"id": "1"}, {"id": "9"}], "has_next_page": True, "next_cursor": "c"}),
            Resp(200, {"tweets": [{"id": "2"}], "has_next_page": False}),
        ])
        client = scrape.Client("test-key", 1000, session=session, sleep=lambda _s: None)
        got = list(client.search("q", stop_if_page=lambda tw: scrape.page_all_known(tw, {"1", "2"})))
        # Page 1 has a new id ("9"), so it is fetched and we do ask for page 2.
        # Page 2 is entirely known, so it is yielded (already paid) and there is no page 3.
        self.assertEqual([t["id"] for t in got], ["1", "9", "2"])
        self.assertEqual(session.calls, 2)
        self.assertTrue(client.last_page_stop)

    def test_connection_error_retries_then_returns_the_page(self):
        closed = requests.exceptions.ConnectionError("Remote end closed connection without response")
        session = FakeSession([
            closed,
            closed,
            Resp(200, {"tweets": [{"id": "9"}], "has_next_page": False}),
        ])
        sleeps = []
        client = scrape.Client("test-key", 100, session=session, sleep=sleeps.append)
        got = list(client.search("q"))
        self.assertEqual([t["id"] for t in got], ["9"])
        self.assertEqual(sleeps, [2, 4])
        self.assertFalse(client.aborted)
        self.assertEqual(client.pages, 1)

    def test_connection_error_gives_up_without_raising(self):
        closed = requests.exceptions.ConnectionError("Remote end closed connection without response")
        session = FakeSession([closed, closed, closed, closed])
        client = scrape.Client("test-key", 100, session=session, sleep=lambda _s: None)
        self.assertEqual(list(client.search("q")), [])
        self.assertTrue(client.aborted)
        self.assertEqual(session.calls, 4)

    def test_hard_cap_does_not_fetch_another_page(self):
        session = FakeSession([
            Resp(200, {"tweets": [{"id": "1"}, {"id": "2"}], "has_next_page": True, "next_cursor": "c"}),
            Resp(200, {"tweets": [{"id": "3"}], "has_next_page": False}),
        ])
        client = scrape.Client("test-key", hard_cap=2, session=session, sleep=lambda _s: None)
        got = list(client.search("q", max_pages=5))
        self.assertEqual([t["id"] for t in got], ["1", "2"])
        self.assertEqual(session.calls, 1)
        self.assertTrue(client.capped)


class CollectionFilterTests(unittest.TestCase):
    def test_scraper_keeps_bot_short_offtopic_and_duplicate_text(self):
        bot = _tweet("b", author={"userName": "noticias24_bot", "isAutomated": True, "followers": 10,
                                  "statusesCount": 100, "createdAt": "Mon Jan 02 10:00:00 +0000 2020"})
        short = _tweet("s", text="ok")
        offtopic = _tweet("o", text="the weather in Paris is lovely today, bring a coat")
        dup_a = _tweet("d1", text="El data center en la Patagonia necesita energía de verdad")
        dup_b = _tweet("d2", text="El data center en la Patagonia necesita energía de verdad")
        for t in (bot, short, offtopic, dup_a, dup_b):
            self.assertIsNone(F.collection_reason(t, n_stored=0, target=1000))
        # The Colab rules still fire. The scraper simply does not call them to drop.
        self.assertEqual(F.content_reason(bot, set()), "bot")
        self.assertEqual(F.content_reason(short, set()), "short")
        self.assertEqual(F.content_reason(offtopic, set()), "offtopic")
        self.assertEqual(F.text_key(dup_a), F.text_key(dup_b))

    def test_retweet_is_the_only_content_drop_and_target_counts_stored_rows(self):
        rt = _tweet("r", retweeted_tweet={"id": "other"})
        self.assertEqual(F.collection_reason(rt, n_stored=0, target=1000), "retweet")
        kept = _tweet("k")
        self.assertIsNone(F.collection_reason(kept, n_stored=999, target=1000))
        self.assertEqual(F.collection_reason(kept, n_stored=1000, target=1000), "over_target")

    def test_on_topic_gates_thread_seeds_only(self):
        outlets = {"clarincom"}
        seed = _tweet("seed", text="Nuevo data center en la Patagonia", replyCount=8,
                      author={"userName": "clarincom", "followers": 10})
        viral = _tweet("viral", text="Pentagon audit, nothing to do with this", replyCount=500,
                       author={"userName": "clarincom", "followers": 2_000_000, "isBlueVerified": True})
        self.assertTrue(scrape.is_thread_seed(seed, outlets))
        self.assertFalse(scrape.is_thread_seed(viral, outlets))
        # The off-topic post is still a keeper for storage.
        self.assertIsNone(F.collection_reason(viral, 0, 1000))

    def test_thread_expansion_skips_a_quiet_known_seed(self):
        seed = _tweet("seed", replyCount=5)
        self.assertTrue(scrape.thread_needs_expansion(seed, known=set(), reply_counts={}))
        self.assertFalse(scrape.thread_needs_expansion(seed, known={"seed"}, reply_counts={"seed": 5}))
        self.assertTrue(scrape.thread_needs_expansion(
            _tweet("seed", replyCount=6), known={"seed"}, reply_counts={"seed": 5}))
        # Stored, but we never recorded a count: check once rather than skip forever.
        self.assertTrue(scrape.thread_needs_expansion(seed, known={"seed"}, reply_counts={}))

    def test_sample_csv_analysis_still_flags_and_mirror_does_not_promote_them(self):
        rows = clean.load_raw_csv(str(ROOT / "notebooks" / "sample_raw.csv"))
        verdicts = {v["tweet_id"]: v["drop_reason"] for v in clean.apply_rules(rows)}
        # Colab-side rules still reproduce the sample's rejects.
        for reason in ("retweet", "bot", "short", "offtopic", "dup_text"):
            self.assertIn(reason, verdicts.values(), reason)
        mirror = [r["tweet_id"] for r in rows if clean.mirror_keep(r, promote_historical=False)]
        promoted = [r["tweet_id"] for r in rows if clean.mirror_keep(r, promote_historical=True)]
        self.assertLess(len(mirror), len(rows))
        self.assertGreater(len(promoted), len(mirror))
        for r in rows:
            reason = r.get("drop_reason")
            if reason in ("bot", "short", "offtopic", "dup_text", "retweet"):
                self.assertNotIn(r["tweet_id"], mirror)
            if reason in (None, "", "over_target"):
                self.assertIn(r["tweet_id"], mirror)

    def test_queries_still_fit_the_length_cap(self):
        queries = scrape.build_queries()
        self.assertGreaterEqual(len(queries), 4)
        for q in queries:
            self.assertIn("-filter:nativeretweets", q)
            self.assertLessEqual(len(q) + 45, 512)


if __name__ == "__main__":
    unittest.main()
