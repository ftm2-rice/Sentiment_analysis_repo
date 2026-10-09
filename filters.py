"""
filters.py — the cleaning rules, as functions.

Both scrape.py and clean.py import this module, so every rule is defined in
exactly one place. The numbers and regexes the rules use live in config.py.

A tweet is judged in this order (see content_reason):

    retweet → bot → short → offtopic → (dedupe, done by the caller)

The caller owns the two stateful checks — "already in the database" and "same
text already seen" — because those depend on what else has been collected.
"""
from __future__ import annotations          # lets us write `str | None` on Python < 3.10

import re                                   # regular expressions (pattern matching in text)
from datetime import datetime, timezone     # dates, always in UTC
from email.utils import parsedate_to_datetime   # parses X's date format ("Tue Sep 01 12:00:00 +0000 2026")

import config as C                          # all thresholds and regexes; referenced as C.SOMETHING

# Every value dc_tweets_raw.drop_reason can hold, in pipeline order.
DROP_REASONS = ["retweet", "bot", "short", "offtopic", "dup_text", "over_target"]
# The reasons that say something about the tweet itself. clean.py re-evaluates these.
# "over_target" only means the scraper's per-run cap was reached; it is not a quality verdict.
CONTENT_REASONS = ["retweet", "bot", "short", "offtopic", "dup_text"]


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


def visible_text(t: dict) -> str:
    """The text a reader actually sees: without the leading @mentions and without links."""
    txt = t.get("text") or ""                               # full text, "" if missing
    rng = t.get("displayTextRange")                         # X gives [start, end] of the visible part
    if isinstance(rng, list) and len(rng) == 2:             # only if it looks like [start, end]
        try:
            txt = txt[rng[0]:rng[1]]                        # slice the string to that range
        except Exception:
            pass                                            # odd values → keep the full text
    txt = re.sub(r"https?://\S+", "", txt)                  # delete every http(s)://… link
    return txt.strip()                                      # remove spaces at both ends


def text_key(t: dict) -> str:
    """A normalised version of the text, used to detect copy-paste reposts."""
    return re.sub(r"\W+", " ", visible_text(t).lower())[:200]   # lowercase, punctuation → space, first 200 chars


def author_is_verified(a: dict) -> bool:
    """Blue check (paid) or any other verification type (organisation, government…)."""
    return bool(a.get("isBlueVerified")) or bool(a.get("verifiedType"))


# ---------------------------------------------------------------------------
# Rule 1 — bots and spam accounts
# ---------------------------------------------------------------------------
def is_bot(author: dict) -> str | None:
    """Return the name of the BOT_RULES entry that fired, or None if the account looks human."""
    R = C.BOT_RULES                                         # the dict of thresholds in config.py
    if R["drop_if_automated_flag"] and author.get("isAutomated"):
        return "automated_flag"                             # X itself labels the account "Automated"
    un = author.get("userName") or ""                       # the @handle, "" if missing
    if re.search(R["drop_if_username_matches"], un):
        return "username_pattern"                           # handle contains bot / _ai / digest / …
    created = parse_dt(author.get("createdAt"))             # when the account was created
    if created:
        age = max((datetime.now(timezone.utc) - created).days, 1)   # account age in days, at least 1
        if age < R["min_account_age_days"]:
            return "account_too_new"                        # younger than the minimum
        if (author.get("statusesCount") or 0) / age > R["max_statuses_per_day"]:
            return "posting_rate"                           # lifetime posts ÷ days alive is too high
    return None                                             # no rule fired


# ---------------------------------------------------------------------------
# Rule 2 — is it really about data centers in Argentina?
# ---------------------------------------------------------------------------
_TOPIC = re.compile(C.TOPIC_RE, re.I)             # data-center words, case-insensitive
_AR = re.compile(C.ARGENTINA_RE, re.I)            # Argentina words (country, provinces, agencies…)
_AR_FALSE = re.compile(C.ARGENTINA_FALSE_RE, re.I)   # phrases where "Argentina" is NOT the place


def topic_matches(text: str) -> list[str]:
    """Every data-center term found in the text (for inspection in the notebook)."""
    return [m.group() for m in _TOPIC.finditer(text or "")]


def argentina_matches(text: str) -> list[str]:
    """Every Argentina term found, excluding those inside a false phrase (than Argentina, carne argentina…)."""
    false_spans = [(m.start(), m.end()) for m in _AR_FALSE.finditer(text or "")]   # character ranges of false phrases
    return [m.group() for m in _AR.finditer(text or "")                            # each Argentina match…
            if not any(s0 <= m.start() < s1 for s0, s1 in false_spans)]            # …unless it starts inside a false span


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
        return True                                         # short text (or outlet): both present is enough
    return any(abs(i - j) <= C.PROXIMITY_CHARS for i in t for j in a)   # long text: some pair must be close


def tweet_topic_text(t: dict) -> str:
    """The text to judge: the tweet's own text plus the quoted tweet's text (quoting an on-topic post counts)."""
    q = t.get("quoted_tweet") or {}                         # the quoted tweet object, or {}
    return f"{t.get('text') or ''}\n{q.get('text') or ''}"  # both texts, joined by a newline


# ---------------------------------------------------------------------------
# All content rules in one call
# ---------------------------------------------------------------------------
def content_reason(t: dict, outlets: set[str], thread_reply: bool = False) -> str | None:
    """Why this tweet should be dropped, or None if it passes every content rule.

    outlets:      lowercase handles treated as news outlets (they get the lenient topic check)
    thread_reply: the tweet was found by expanding an on-topic outlet thread; such replies
                  don't have to repeat the keywords, so the off-topic rule is skipped.
    Dedupe is NOT done here: it needs the set of texts already kept, which the caller holds.
    """
    if t.get("retweeted_tweet"):
        return "retweet"                                    # a plain RT: someone else's words
    a = t.get("author") or {}                               # the author object, or {}
    if is_bot(a):
        return "bot"                                        # rule 1
    if len(visible_text(t)) < C.BOT_RULES["min_text_chars"]:
        return "short"                                      # too little text to hold an opinion
    if not thread_reply and not is_on_topic(
            tweet_topic_text(t), lenient=(a.get("userName") or "").lower() in outlets):
        return "offtopic"                                   # rule 2 (outlets judged leniently)
    return None                                             # passed


# ---------------------------------------------------------------------------
# Rule 3 — who is speaking (segment / source_type)
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
