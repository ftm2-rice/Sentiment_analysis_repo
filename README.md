# Sentiment analysis — data centers in Argentina

Collects public posts from X about data centers in Argentina, cleans them with
explicit rules, and stores them in Supabase for the analysis
([Streamlit app](https://github.com/ftm2-rice/Streamlit_visual_sentiment_analysis_dc)).

```
config.py      every threshold, regex, outlet list and query template — edit this, not the code
filters.py     the cleaning rules as functions (one definition, used by both scripts)
scrape.py      fetches from twitterapi.io; writes EVERYTHING fetched to dc_tweets_raw
               (with drop_reason) and the survivors to dc_tweets
clean.py       re-applies the rules to dc_tweets_raw → dc_tweets; writes out/pipeline_summary.json
sql/           one-off migrations (run in the Supabase SQL editor)
notebooks/     pipeline.ipynb — the whole process, explained, runnable on a CSV export
```

## Run

```
python scrape.py --mode daily            # cron: new tweets + history until 1,000 clean rows
python scrape.py --mode test --dry-run --csv out/test.csv   # ≈ $0.015, writes a raw CSV only
python clean.py                          # rebuild dc_tweets from dc_tweets_raw
python clean.py --csv out/test.csv --dry-run                # rules on a CSV, no Supabase
```

Env vars: `TWITTERAPI_IO_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (GitHub Actions secrets).
Never commit credentials to this repository.

## Tables

| table | what | written by |
|---|---|---|
| `dc_tweets_raw` | every tweet fetched; `drop_reason` NULL = passed | scrape.py, clean.py |
| `dc_tweets` | clean rows the analysis uses | scrape.py, clean.py |
| `dc_runs` | one row per run: fetched, inserted, cost, drop counts | scrape.py |
| `dc_coverage` | ISO-week × query cells already swept | scrape.py |

First-time setup for the raw table: run `sql/001_dc_tweets_raw.sql` once.

## Changing a rule

1. Edit `config.py` (or a function in `filters.py`).
2. `python clean.py --dry-run` shows how many rows change verdict.
3. `python clean.py` applies it. Nothing is re-fetched; you already paid for the raw rows.
