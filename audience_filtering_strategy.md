# Audience and collection gates (`config.py`, `gates.py`)

GitHub Actions collects. The Colab notebook filters.

This file describes what **this repository** decides. Bot, short-text, off-topic, and duplicate-text rules are not implemented here. They run in the Colab notebook that reads `dc_tweets_raw`.

---

## 1. What the search asks for

Every query is a data-center term **and** an Argentina anchor (`CORE_TERMS`, `ARGENTINA_ANCHORS`). Company names are only used together with both. `-filter:nativeretweets` drops plain retweets before we pay for them. A retweet the API still returns is stored in `dc_tweets_raw` with `drop_reason = retweet` and is not copied to `dc_tweets`.

Ids already in `dc_tweets` are not stored again.

## 2. Thread expansion

`gates.is_on_topic` is a cost gate, not a drop. An outlet post (or a large verified account) is expanded only when its own text has a data-center term and an Argentina term. For a text longer than `LONG_TEXT_CHARS` (600), those terms must sit within `PROXIMITY_CHARS` (200). A mention that is only inside `ARGENTINA_FALSE_RE` (comparison, commodity, sports) does not count, so that thread is not expanded. The tweet is still stored.

## 3. Who is speaking (`source_type`)

`gates.classify()` labels every stored tweet. The label is not a filter.

1. **`outlet_post`** — headline post of a configured outlet or official account.
2. **`news_reply`** / **`news_quote`** — direct reply to, or quote of, an outlet post.
3. **`news_thread`** — another reply found by expanding an outlet thread.
4. **`verified_opinion`** — standalone post by a verified account with at least `VERIFIED_MIN_FOLLOWERS` (2,000).
5. **`general_public`** — everyone else.

An account that is not on the outlet list is treated as one for expansion when it is verified, has at least `AUTO_OUTLET_MIN_FOLLOWERS` (50,000), and its on-topic post has at least `AUTO_OUTLET_MIN_REPLIES` (5) replies.

## 4. What Colab does, not this repo

After the daily run, the Colab notebook reads `dc_tweets_raw` and sets aside bots, short texts, off-topic posts, and duplicate texts, then builds the TF/TF-IDF tables the Streamlit app displays. Changing those rules does not require a change here, and this repo will not re-apply them.
