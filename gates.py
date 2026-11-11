"""
gates.py — what the collector checks, and how a tweet becomes a database row.

Nothing here decides bot / short / off-topic / duplicate-text. That filtering
lives in the Colab notebook that reads dc_tweets_raw.

This module does three jobs:

  * collection_reason — drop a plain retweet (the only content drop)
  * is_on_topic       — cost gate: only expand a thread if the outlet post
                        itself is about data centers in Argentina
  * classify / to_row — label source_type and flatten the API object

Thresholds and regexes live in config.py.
"""
from __future__ import annotations          # lets us write `str | None` on Python < 3.10

import re                                   # regular expressions (pattern matching in text)
from datetime import datetime, timezone     # dates, always in UTC
from email.utils import parsedate_to_datetime   # parses X's date format ("Tue Sep 01 12:00:00 +0000 2026")

import config as C                          # all thresholds and regexes; referenced as C.SOMETHING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_dt(s: str | None) -> datetime | None:
    """Turn a date string from the API into a datetime, or None if it can't be parsed."""
    if not s:                                               # empty / missing → nothing to parse
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)   # X's usual format
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO format (our own exports)
        except Exception:
            return None                                     # neither worked → give up quietly


def author_is_verified(a: dict) -> bool:
    """Blue check (paid) or any other verification type (organisation, government…)."""
    return bool(a.get("isBlueVerified")) or bool(a.get("verifiedType"))


# ---------------------------------------------------------------------------
# Thread-seed gate — is this outlet post about data centers in Argentina?
# Used only to decide which threads are worth paying to expand. A tweet that
# fails this check is still stored.
# ---------------------------------------------------------------------------
_TOPIC = re.compile(C.TOPIC_RE, re.I)             # data-center words, case-insensitive
_AR = re.compile(C.ARGENTINA_RE, re.I)            # Argentina words (country, provinces, agencies…)
_AR_FALSE = re.compile(C.ARGENTINA_FALSE_RE, re.I)   # phrases where "Argentina" is NOT the place


def is_on_topic(text: str, lenient: bool = False) -> bool:
    """Data-center term AND Argentina term; and, if the text is long, close to each other."""
    if not text:
        return False                                        # nothing to judge
    t = [m.start() for m in _TOPIC.finditer(text)]          # positions of data-center terms
    false_spans = [(m.start(), m.end()) for m in _AR_FALSE.finditer(text)]        # ranges of false phrases
    a = [m.start() for m in _AR.finditer(text)                                     # positions of Argentina terms…
         if not any(s0 <= m.start() < s1 for s0, s1 in false_spans)]               # …outside false phrases
    if not t or not a:
        return False                                        # one of the two is missing
    if lenient or len(text) <= C.LONG_TEXT_CHARS:
        return True                                         # short text (or lenient caller): both present is enough
    return any(abs(i - j) <= C.PROXIMITY_CHARS for i in t for j in a)   # long text: some pair must be close


def tweet_topic_text(t: dict) -> str:
    """The text to judge: the tweet's own text plus the quoted tweet's text (quoting an on-topic post counts)."""
    q = t.get("quoted_tweet") or {}                         # the quoted tweet object, or {}
    return f"{t.get('text') or ''}\n{q.get('text') or ''}"  # both texts, joined by a newline


def collection_reason(t: dict) -> str | None:
    """The only content drop at collection time, or None if the tweet is stored.

    A plain retweet is someone else's words. Bot, short text, off-topic and
    duplicate text are not decided here.
    """
    if t.get("retweeted_tweet"):
        return "retweet"
    return None


# ---------------------------------------------------------------------------
# Who is speaking (segment / source_type) — a label, not a drop
# ---------------------------------------------------------------------------
def classify(t: dict, outlets: set[str], thread_of: str | None = None) -> tuple[str, str | None]:
    """Return (source_type, outlet_handle). thread_of = the outlet whose thread the tweet was found in."""
    a = t.get("author") or {}
    un = (a.get("userName") or "").lower()                  # author handle, lowercase
    if un in outlets and not t.get("isReply"):
        return "outlet_post", un                            # the outlet's own headline: information, not opinion
    reply_to = (t.get("inReplyToUsername") or "").lower()   # who the tweet replies to
    if t.get("isReply") and reply_to in outlets:
        return "news_reply", reply_to                       # a direct reply to an outlet
    q = t.get("quoted_tweet") or {}
    q_user = ((q.get("author") or {}).get("userName") or "").lower()   # author of the quoted tweet
    if q_user and q_user in outlets:
        return "news_quote", q_user                         # quote-repost of an outlet's post
    if thread_of:
        return "news_thread", thread_of                     # reply inside an outlet's thread, to another commenter
    if author_is_verified(a) and (a.get("followers") or 0) >= C.VERIFIED_MIN_FOLLOWERS:
        return "verified_opinion", None                     # standalone post by a verified account with reach
    return "general_public", None                           # everyone else


# ---------------------------------------------------------------------------
# Row builder — the columns of dc_tweets (dc_tweets_raw adds a few on top)
# ---------------------------------------------------------------------------
def to_row(t: dict, source_type: str, outlet: str | None, query: str, mode: str) -> dict:
    """Flatten the API object into one database row."""
    a = t.get("author") or {}                               # author object
    q = t.get("quoted_tweet") or {}                         # quoted tweet object
    ac = parse_dt(a.get("createdAt"))                       # account creation date
    return {
        "tweet_id": str(t["id"]),                                           # primary key
        "created_at": (parse_dt(t.get("createdAt")) or datetime.now(timezone.utc)).isoformat(),  # when posted
        "text": t.get("text") or "",                                        # full text
        "lang": t.get("lang"),                                              # language X detected (es, en…)
        "url": t.get("url"),                                                # link to the post
        "author_id": a.get("id"),
        "author_username": a.get("userName"),                               # @handle
        "author_name": a.get("name"),                                       # display name
        "author_followers": a.get("followers"),
        "author_following": a.get("following"),
        "author_statuses": a.get("statusesCount"),                          # lifetime number of posts
        "author_created_at": ac.isoformat() if ac else None,
        "author_verified": author_is_verified(a),
        "author_verified_type": a.get("verifiedType") or None,
        "author_is_automated": bool(a.get("isAutomated")),
        "author_description": a.get("description"),                        # bio
        "author_location": a.get("location"),                               # free-text location in the bio
        "like_count": t.get("likeCount"),
        "retweet_count": t.get("retweetCount"),
        "reply_count": t.get("replyCount"),
        "quote_count": t.get("quoteCount"),
        "view_count": t.get("viewCount"),
        "is_reply": bool(t.get("isReply")),
        "in_reply_to_id": t.get("inReplyToId") or None,
        "in_reply_to_username": t.get("inReplyToUsername") or None,
        "conversation_id": t.get("conversationId") or None,                 # id of the thread it belongs to
        "quoted_tweet_id": q.get("id"),
        "quoted_username": (q.get("author") or {}).get("userName"),
        "source_type": source_type,                                         # segment from classify()
        "outlet_handle": outlet,                                            # which outlet, if any
        "matched_query": query[:500],                                       # the search string that found it
        "run_mode": mode,                                                   # daily / test / backfill / bonus
        "raw": t,                                                           # the whole API object (JSON column)
    }
