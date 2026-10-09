"""
All the knobs live here. Edit this file, not scrape.py.
"""

# ---------------------------------------------------------------------------
# Topic definition
# ---------------------------------------------------------------------------
# Every query is  CORE_TERMS AND (ARGENTINA_ANCHORS)  so a tweet must mention a
# data center *and* something that ties it to Argentina. Company names are
# never used alone — "OpenAI" without "data center" would pull unrelated noise.

CORE_TERMS = (
    '("data center" OR "data centers" OR datacenter OR datacenters '
    'OR "centro de datos" OR "centros de datos" OR "centro de cómputo")'
)

ARGENTINA_ANCHORS = (
    '(Argentina OR argentino OR argentina OR argentinos OR Patagonia '
    'OR "Río Negro" OR "Rio Negro" OR "Sierra Grande" OR "Bahía Blanca" '
    'OR "Buenos Aires" OR Córdoba OR Mendoza OR Neuquén OR "Vaca Muerta" OR Añelo '
    'OR RIGI OR "Secretaría de Energía" OR CAMMESA OR ENARSA)'
)
# NOTE: Stargate / Milei / Caputo were removed as anchors — "Stargate" alone
# pulled in the US Stargate (Abilene, New Mexico) and Milei pulled in jokes.

# ---------------------------------------------------------------------------
# Post-filter applied in code to EVERY tweet the search returns (X search
# matches anywhere in long posts, so a 2,000-char digest mentioning "data
# centers" once and "Argentina" once elsewhere gets through). A tweet passes
# only if a data-center term and an Argentina term both appear, and for long
# texts they must sit within PROXIMITY_CHARS of each other.
# ---------------------------------------------------------------------------
TOPIC_RE = r"data\s?-?cent(?:er|re)s?|datacenters?|centros?\s+de\s+(?:datos|c[oó]mputo)|#centrosdedatos"
ARGENTINA_RE = (
    r"argentin|patagoni|r[ií]o\s+negro|sierra\s+grande|bah[ií]a\s+blanca|buenos\s+aires|"
    r"c[oó]rdoba|mendoza|neuqu[eé]n|vaca\s+muerta|a[ñn]elo|\brigi\b|secretar[ií]a\s+de\s+energ[ií]a|"
    r"cammesa|enarsa|resoluci[oó]n\s+264|\bxdem\b|\bnoa\b|tierra\s+del\s+fuego|chubut|santa\s+cruz|"
    r"san\s+juan|\bsalta\b|jujuy|catamarca|tucum[aá]n|rosario|la\s+plata"
)
# \bsalta\b: without the word boundary "resalta" / "asalta" counted as the province.
# Known remaining ambiguities (kept on purpose, check by hand in the notebook):
# "Córdoba" and "Santa Cruz" also exist outside Argentina; "Mendoza" and "Rosario" are surnames.
LONG_TEXT_CHARS = 600      # above this, require proximity
PROXIMITY_CHARS = 200   # outlet accounts (OUTLETS) bypass this rule

# Phrases where "Argentina" is only a comparison unit or a commodity, not the place
# the data center is in. A tweet whose ONLY Argentina mention is one of these is dropped.
ARGENTINA_FALSE_RE = (
    r"than\s+argentina|beef\s+from\s+argentina|argentin\w*\s+beef|carne\s+argentina|"
    r"argentina\s+(?:vs|v\.?)\s|messi|mundial|world\s+cup"
)

# Company-driven news hooks. Still gated by CORE_TERMS, so strictly data-center.
COMPANY_ANCHORS = (
    '(OpenAI OR Altman OR Microsoft OR Google OR AWS OR Amazon OR Meta '
    'OR Nvidia OR Oracle OR Cirion OR Telecom OR Claro OR Equinix)'
)

# Base search strings. {core}/{ar}/{co}/{to_outlets} are filled in at runtime.
# -filter:nativeretweets drops plain RTs (they're not opinions).
# X caps a search at ~512 chars; scrape.py splits {to_outlets} into chunks.
SEARCH_QUERIES = [
    "{core} {ar} lang:es -filter:nativeretweets",
    "{core} {co} (Argentina OR argentino OR Patagonia OR RIGI) lang:es -filter:nativeretweets",
    "{core} (Argentina OR Argentine OR Patagonia) lang:en -filter:nativeretweets",
    # replies to outlets that mention the topic themselves
    "{core} ({to_outlets}) -filter:nativeretweets",
]
TO_OUTLETS_PER_QUERY = 12

# ---------------------------------------------------------------------------
# News outlets / verified accounts used to tag "news_reply" and to expand
# threads. Handles without the @. Verified ones as of Sept 2026 — see README
# for the ones you should double-check.
# ---------------------------------------------------------------------------
OUTLETS = [
    # national dailies / portals
    "clarincom", "LANACION", "infobae", "Ambitocom", "pagina12",
    "iProfesional", "cronistacom", "perfilcom", "eldestapeweb",
    "LPOArgentina", "ElCanciller", "BAEnegocios", "ElEconomista_",
    "Chequeado", "letrap_ar",
    # TV / radio news
    "todonoticias", "C5N", "A24COM", "LN_Mas", "radiomitre", "Continental590",
    # business / tech
    "bloomberglinea", "ForbesArgentina", "iProUP", "Infotechnology",
    "TNTecno", "Convergencia_", "TeleSemana",
    # regional (Patagonia / Río Negro / Córdoba)
    "rionegrocomar", "LMNeuquen", "LaVozdelInterior", "lacapital",
    "LaGacetaTucuman", "diariouno", "losandesdiario", "lanuevaweb",
    # (international wires removed on purpose: to:Reuters pulled US/China news)
    # official / corporate accounts that drive the conversation
    "JMilei", "OPRArgentina", "MinEconomia_Ar", "CasaRosada", "ARCA_Argentina", "Voceria_Ar",
    # energy / tech trade press that covered the 264/2026 resolution
    "econojournal", "dpl_news", "Minergyar", "enriquecarrier",
]

# Any *other* account that the sweep finds posting about the topic is treated
# like an outlet if it meets both of these. This is how "verified ones I
# haven't included" get picked up automatically.
AUTO_OUTLET_MIN_FOLLOWERS = 50_000
AUTO_OUTLET_MIN_REPLIES = 5      # its post must have generated discussion
# …and (enforced in code) the post itself must pass TOPIC_RE + ARGENTINA_RE.
# Otherwise a viral off-topic post (e.g. a Pentagon-audit thread) gets expanded.

# Threshold for tagging a standalone post as "verified_opinion"
VERIFIED_MIN_FOLLOWERS = 2_000

# The segments (source_type) assigned by filters.classify(), with the wording
# used downstream (notebook, Streamlit glossary). Edit here, not in app.py.
SOURCE_TYPES = {
    "outlet_post":      "headline post of a news outlet or official account — information, not opinion",
    "news_reply":       "direct reply to an outlet's post",
    "news_quote":       "quote-repost of an outlet's post with the person's own comment",
    "news_thread":      "reply inside an outlet's thread, addressed to another commenter",
    "verified_opinion": f"standalone post by a verified account with ≥ {VERIFIED_MIN_FOLLOWERS:,} followers",
    "general_public":   "everyone else",
}
OPINION_TYPES = [k for k in SOURCE_TYPES if k != "outlet_post"]   # what sentiment is scored on

# ---------------------------------------------------------------------------
# Bot / spam filter. A tweet is dropped if its author trips any of these.
# ---------------------------------------------------------------------------
BOT_RULES = {
    "drop_if_automated_flag": True,          # X's own "Automated" label
    "drop_if_username_matches": r"(?i)(bot|_ia$|_ai$|ainew|noticias24|news24|digest|podcast_|summary)",
    "max_statuses_per_day": 150,             # tweets/day over account lifetime
    "min_account_age_days": 14,
    "drop_if_no_text_after_mentions": True,  # replies that are only @handles / links
    "min_text_chars": 25,
}

# ---------------------------------------------------------------------------
# Volume / cost controls (twitterapi.io: ~$0.15 per 1,000 tweets returned)
# ---------------------------------------------------------------------------
# Earliest week worth scraping — before this the topic barely existed on X.
HISTORY_START = "2024-12-01"
WINDOW_DAYS = 7          # ISO weeks (Monday 00:00 UTC → next Monday)

DAILY = {
    # Phase 1: everything new in the last 48 h (overlap on purpose; dedupe handles it)
    "lookback_hours": 48,
    "fresh_pages_per_query": 2,         # 48 h never yields more than ~40 per query
    # Phase 2: walk backwards one ISO week at a time (coverage ledger skips
    # weeks already done) until target_new_tweets clean rows are collected.
    "start_date": HISTORY_START,
    "max_pages_per_query_window": 4,    # 80 tweets per query per week
    "target_new_tweets": 1000,          # CLEAN rows (after filters + dedupe).
                                        # Once dc_tweets has ~4,000 rows, drop this to 200:
                                        # the cron then just keeps the dataset current.
    "max_thread_pages": 3,
    "max_threads": 40,
    "hard_cap_tweets_fetched": 2500,    # ≈ 37.5k credits ≈ $0.375 worst case per run
}

# Global spend guard: the script refuses to start once the sum of est_cost_usd in
# dc_runs reaches this. Override with env BUDGET_USD. $20 recharged → keep $2 margin.
BUDGET_USD = 18.0

# One-off deep sweep (same engine as daily's phase 2, no fresh phase, bigger caps)
BACKFILL = {
    "start_date": HISTORY_START,
    "target_new_tweets": 5000,
    "max_pages_per_query_window": 8,
    "max_thread_pages": 4,
    "max_threads": 150,
    "hard_cap_tweets_fetched": 15000,   # ≈ 225k credits ≈ $2.25 worst case
}

# Smoke test: validates the whole pipeline for ~1,500 credits (≈ $0.015)
TEST = {
    "lookback_hours": 24 * 14,
    "fresh_pages_per_query": 1,
    "max_pages_per_query_window": 1,
    "target_new_tweets": 40,
    "max_thread_pages": 1,
    "max_threads": 2,
    "hard_cap_tweets_fetched": 100,
}

# Squeeze a 10k-credit bonus account: history only, stops at ~640 fetched.
BONUS = {
    "start_date": HISTORY_START,
    "target_new_tweets": 10_000,
    "max_pages_per_query_window": 1,
    "max_thread_pages": 1,
    "max_threads": 6,
    "hard_cap_tweets_fetched": 640,
}

MODES = {"backfill": BACKFILL, "daily": DAILY, "test": TEST, "bonus": BONUS}

SUPABASE_TABLE = "dc_tweets"              # clean rows: what the analysis uses
SUPABASE_RAW_TABLE = "dc_tweets_raw"      # everything fetched, with drop_reason (NULL = kept)
SUPABASE_RUNS_TABLE = "dc_runs"
SUPABASE_COVERAGE_TABLE = "dc_coverage"
