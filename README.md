# Sentiment analysis — data centers in Argentina

Collects public posts from X about data centers in Argentina and stores them in
Supabase. **The scraper collects (cost-saving gates only). The Colab notebook
filters** (bot, short text, off-topic, duplicate text) when it reads
`dc_tweets_raw`.

The [Streamlit app](https://github.com/ftm2-rice/Streamlit_visual_sentiment_analysis_dc)
does not query Supabase live. It shows a snapshot built from `dc_tweets`. That
table is the scraper's kept rows (raw minus retweets), not the Colab-filtered set.

```
config.py      every threshold, regex, outlet list and query template — edit this, not the code
filters.py     collection_reason (scraper) and content_reason (Colab analysis)
scrape.py      fetches from twitterapi.io; writes everything to dc_tweets_raw and
               the non-retweets to dc_tweets. Flushes after each query.
clean.py       mirrors dc_tweets_raw → dc_tweets (no analysis filters, no deletes)
sql/           one-off migrations (run in the Supabase SQL editor)
notebooks/     pipeline.ipynb — the queries, the raw table, and the Colab filters, on a CSV
```

## What the daily run spends money on

Cost-saving gates, in `scrape.py` / `config.py`:

* search query: `CORE_TERMS` and an Argentina anchor, `-filter:nativeretweets`
* skip tweet ids already in `dc_tweets_raw`
* stop a Latest query when a whole page is ids we already have
* `backfill_history` is **off** for `--mode daily` (history is collected; use `--mode backfill`)
* fresh window is ~26 h, or just after the last finished run's watermark
* expand a thread only when the post is new or `reply_count` grew
* `is_on_topic` decides which outlet posts are worth expanding — it does not drop the tweet
* `BUDGET_USD` and the per-run hard cap

`target_new_tweets` counts **new stored rows** (non-retweets), not rows that survived an analysis filter.

## Run

```
python scrape.py --mode daily            # cron: new tweets since the last finished run
python scrape.py --mode backfill         # walk historical ISO weeks (coverage ledger skips done cells)
python scrape.py --mode test --dry-run --csv out/test.csv   # ≈ $0.015, writes a raw CSV only
python clean.py                          # mirror scraper-kept rows into dc_tweets; deletes nothing
python clean.py --csv out/test.csv --dry-run                # counts only, no Supabase
```

Env vars: `TWITTERAPI_IO_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (GitHub Actions secrets).
Never commit credentials to this repository.

## Tables

| table | what | written by |
|---|---|---|
| `dc_tweets_raw` | every tweet fetched; `drop_reason` NULL = scraper kept it | scrape.py |
| `dc_tweets` | scraper-kept rows (raw minus retweets). Streamlit's snapshot is built from this | scrape.py, clean.py |
| `dc_runs` | one row per run: fetched, inserted, cost, per-query yield | scrape.py |
| `dc_coverage` | ISO-week × query cells already swept (backfill) | scrape.py |

`drop_reason` values the scraper writes now: NULL, `retweet`, `over_target`.
Older rows may still say `bot`, `short`, `offtopic` or `dup_text`. Those are not
copied into `dc_tweets` unless you run `python clean.py --promote-historical`
(that changes the pool a future Streamlit export would see).

First-time setup for the raw table: run `sql/001_dc_tweets_raw.sql` once.

## Changing an analysis rule

Edit `config.py` or `filters.py` and re-run the Colab notebook on `dc_tweets_raw`.
Nothing is re-fetched; you already paid for the raw rows. `clean.py` will not
apply that change to `dc_tweets`.
