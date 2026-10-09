-- dc_tweets_raw: everything the scraper fetched, including the rows the rules
-- rejected. Same columns as dc_tweets plus:
--   drop_reason   NULL = passed every rule; otherwise retweet | bot | short |
--                 offtopic | dup_text | over_target (see filters.DROP_REASONS)
--   found_via     'search' (keyword sweep) or 'thread' (expanded under an outlet post)
--   thread_of     outlet handle whose thread the tweet was found in
--   run_id        dc_runs.id of the run that fetched it
--   legacy        true for rows copied from dc_tweets before this table existed
--                 (they passed the rules in force when collected; their rejected
--                 siblings were not stored at the time)
--   recleaned_at  last time clean.py changed this row's verdict
--
-- Run once in the Supabase SQL editor. Safe to re-run: every statement is guarded.

create table if not exists dc_tweets_raw (like dc_tweets including all);

alter table dc_tweets_raw
  add column if not exists drop_reason  text,
  add column if not exists found_via    text not null default 'search',
  add column if not exists thread_of    text,
  add column if not exists run_id       bigint,
  add column if not exists legacy       boolean not null default false,
  add column if not exists recleaned_at timestamptz;

create index if not exists dc_tweets_raw_drop_reason_idx on dc_tweets_raw (drop_reason);
create index if not exists dc_tweets_raw_run_id_idx      on dc_tweets_raw (run_id);

-- Seed with what we already have: every current clean row is a raw row that passed.
insert into dc_tweets_raw
select t.*, null, 'search', null, null, true, null
from dc_tweets t
on conflict (tweet_id) do nothing;

-- Rows that were found via thread expansion carry source_type = 'news_thread'
-- and outlet_handle = the outlet; recover found_via / thread_of from that.
update dc_tweets_raw
   set found_via = 'thread', thread_of = outlet_handle
  where legacy and source_type in ('news_reply', 'news_quote', 'news_thread') and thread_of is null;
