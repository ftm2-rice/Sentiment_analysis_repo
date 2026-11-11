# Sentiment analysis — data centers in Argentina

Collects public posts from X about data centers in Argentina and stores them in
Supabase. **The scraper collects. The Colab notebook filters** (bot, short text,
off-topic, duplicate text) when it reads `dc_tweets_raw`.

The [Streamlit app](https://github.com/ftm2-rice/Streamlit_visual_sentiment_analysis_dc)
reads a snapshot built from `dc_tweets`, which is scraper-kept rows (raw minus retweets),
not the Colab-filtered set.

```
config.py      every threshold, regex, outlet list and query template — edit this, not the code
filters.py     collection_reason (scraper) and content_reason (Colab)
scrape.py      fetches from twitterapi.io; writes everything to dc_tweets_raw and
               non-retweets to dc_tweets. Drops retweets and known ids only.
clean.py       mirrors scraper-kept rows into dc_tweets; does not re-apply Colab filters
sql/           one-off migrations (run in the Supabase SQL editor)
notebooks/     pipeline.ipynb — queries, raw table, and the Colab filters, on a CSV
```

## Run

```
python scrape.py --mode daily            # cron: new tweets + history until 1,000 stored rows
python scrape.py --mode test --dry-run --csv out/test.csv   # ≈ $0.015, writes a raw CSV only
python clean.py                          # mirror scraper-kept rows into dc_tweets
python clean.py --csv out/test.csv --dry-run                # counts only, no Supabase
```

Env vars: `TWITTERAPI_IO_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (GitHub Actions secrets).
Never commit credentials to this repository.

## Tables

| table | what | written by |
|---|---|---|
| `dc_tweets_raw` | every tweet fetched; `drop_reason` NULL = scraper kept it | scrape.py, clean.py |
| `dc_tweets` | scraper-kept rows (raw minus retweets) | scrape.py, clean.py |
| `dc_runs` | one row per run: fetched, inserted, cost, drop counts | scrape.py |
| `dc_coverage` | ISO-week × query cells already swept | scrape.py |

First-time setup for the raw table: run `sql/001_dc_tweets_raw.sql` once.

## Changing an analysis rule

Edit `config.py` or `filters.content_reason` and re-run the Colab notebook on
`dc_tweets_raw`. Nothing is re-fetched. `clean.py` will not apply that change
to `dc_tweets`. Historical rows already tagged `bot`, `short`, `offtopic` or
`dup_text` stay as stored.
