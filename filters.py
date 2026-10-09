"""
The cleaning rules, as functions. Both scrape.py and clean.py import this
module, so a rule is defined exactly once. All thresholds come from config.py.

Order in which a tweet is judged (content_reason):

    retweet  → bot → short → offtopic → (dedupe, done by the caller)

The caller (scrape.py or clean.py) owns the stateful checks — "already in the
database" and "same text already seen" — because those depend on what else
has been collected.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import config as C

# Every reason a row can carry in dc_tweets_raw.drop_reason, in pipeline order.
DROP_REASONS = ["retweet", "bot", "short", "offtopic", "dup_text", "over_target"]
# Reasons that say something about the tweet's content (clean.py re-evaluates
# these). "over_target" only means the scraper's per-run cap was hit.
CONTENT_REASONS = ["retweet", "bot", "short", "offtopic", "dup_text"]


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
    """Text with leading @mentions and links stripped (what the reader actually sees)."""
    txt = t.get("text") or ""
    rng = t.get("displayTextRange")
    if isinstance(rng, list) and len(rng) == 2:
        try:
            txt = txt[rng[0]:rng[1]]
        except Exception:
            pass
    txt = re.sub(r"https?://\S+", "", txt)
    return txt.strip()


def text_key(t: dict) -> str:
    """Normalised text used to collapse copy-paste reposts by different accounts."""
    return re.sub(r"\W+", " ", visible_text(t).lower())[:200]


def author_is_verified(a: dict) -> bool:
    return bool(a.get("isBlueVerified")) or bool(a.get("verifiedType"))


# ---------------------------------------------------------------------------
# Rule 1 — bots and spam accounts
# ---------------------------------------------------------------------------
def is_bot(author: dict) -> str | None:
    """Return the name of the BOT_RULES entry that fired, or None."""
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


# ---------------------------------------------------------------------------
# Rule 2 — is it really about data centers in Argentina?
# ---------------------------------------------------------------------------
_TOPIC = re.compile(C.TOPIC_RE, re.I)
_AR = re.compile(C.ARGENTINA_RE, re.I)
_AR_FALSE = re.compile(C.ARGENTINA_FALSE_RE, re.I)


def topic_matches(text: str) -> list[str]:
    return [m.group() for m in _TOPIC.finditer(text or "")]


def argentina_matches(text: str) -> list[str]:
    """Argentina mentions that are NOT inside a false phrase (than Argentina, carne argentina…)."""
    false_spans = [(m.start(), m.end()) for m in _AR_FALSE.finditer(text or "")]
    return [m.group() for m in _AR.finditer(text or "")
            if not any(s0 <= m.start() < s1 for s0, s1 in false_spans)]


def is_on_topic(text: str, lenient: bool = False) -> bool:
    """Data-center term AND Argentina term; close together if the text is long."""
    if not text:
        return False
    t = [m.start() for m in _TOPIC.finditer(text)]
    false_spans = [(m.start(), m.end()) for m in _AR_FALSE.finditer(text)]
    a = [m.start() for m in _AR.finditer(text)
         if not any(s0 <= m.start() < s1 for s0, s1 in false_spans)]
    if not t or not a:
        return False
    if lenient or len(text) <= C.LONG_TEXT_CHARS:
        return True
    return any(abs(i - j) <= C.PROXIMITY_CHARS for i in t for j in a)


def tweet_topic_text(t: dict) -> str:
    """Own text plus quoted tweet's text (a quote of an on-topic post counts)."""
    q = t.get("quoted_tweet") or {}
    return f"{t.get('text') or ''}\n{q.get('text') or ''}"


# ---------------------------------------------------------------------------
# All content rules in one call
# ---------------------------------------------------------------------------
def content_reason(t: dict, outlets: set[str], thread_reply: bool = False) -> str | None:
    """Why this tweet should be dropped, or None if it passes every content rule.

    thread_reply: the tweet was found by expanding an on-topic outlet thread; such
    replies don't have to repeat the keywords, so the off-topic rule is skipped.
    Dedupe is NOT done here (it needs the set of texts already kept).
    """
    if t.get("retweeted_tweet"):
        return "retweet"
    a = t.get("author") or {}
    if is_bot(a):
        return "bot"
    if len(visible_text(t)) < C.BOT_RULES["min_text_chars"]:
        return "short"
    if not thread_reply and not is_on_topic(
            tweet_topic_text(t), lenient=(a.get("userName") or "").lower() in outlets):
        return "offtopic"
    return None


# ---------------------------------------------------------------------------
# Rule 3 — who is speaking (segment)
# ---------------------------------------------------------------------------
def classify(t: dict, outlets: set[str], thread_of: str | None = None) -> tuple[str, str | None]:
    """Return (source_type, outlet_handle). thread_of = outlet whose thread the tweet was found in."""
    a = t.get("author") or {}
    un = (a.get("userName") or "").lower()
    if un in outlets and not t.get("isReply"):
        return "outlet_post", un                       # headline, not opinion
    reply_to = (t.get("inReplyToUsername") or "").lower()
    if t.get("isReply") and reply_to in outlets:
        return "news_reply", reply_to
    q = t.get("quoted_tweet") or {}
    q_user = ((q.get("author") or {}).get("userName") or "").lower()
    if q_user and q_user in outlets:
        return "news_quote", q_user
    if thread_of:
        return "news_thread", thread_of                # reply inside an outlet's thread, to another commenter
    if author_is_verified(a) and (a.get("followers") or 0) >= C.VERIFIED_MIN_FOLLOWERS:
        return "verified_opinion", None
    return "general_public", None


# ---------------------------------------------------------------------------
# Row builder (schema of dc_tweets; dc_tweets_raw adds a few columns on top)
# ---------------------------------------------------------------------------
def to_row(t: dict, source_type: str, outlet: str | None, query: str, mode: str) -> dict:
    a = t.get("author") or {}
    q = t.get("quoted_tweet") or {}
    ac = parse_dt(a.get("createdAt"))
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
        "author_created_at": ac.isoformat() if ac else None,
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
