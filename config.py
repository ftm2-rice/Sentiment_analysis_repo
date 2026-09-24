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
    'OR "Buenos Aires" OR Córdoba OR Mendoza OR Neuquén OR "Vaca Muerta" '
    'OR Stargate OR RIGI OR Milei OR Caputo OR "Sur Energy")'
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
    "{core} {co} (Argentina OR argentino OR Patagonia OR Stargate OR RIGI) lang:es -filter:nativeretweets",
    "{core} (Argentina OR Patagonia OR Stargate) lang:en -filter:nativeretweets",
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
    # wires
    "Reuters", "AFPespanol", "EFEnoticias", "bbcmundo", "CNNEE",
    # official / corporate accounts that drive the conversation
    "OpenAI", "sama", "JMilei", "OPRArgentina", "MinEconomia_Ar",
    "CasaRosada", "ARCA_Argentina",
]

# Any *other* account that the sweep finds posting about the topic is treated
# like an outlet if it meets both of these. This is how "verified ones I
# haven't included" get picked up automatically.
AUTO_OUTLET_MIN_FOLLOWERS = 50_000
AUTO_OUTLET_MIN_REPLIES = 5      # its post must have generated discussion

# Threshold for tagging a standalone post as "verified_opinion"
VERIFIED_MIN_FOLLOWERS = 2_000

# ---------------------------------------------------------------------------
# Bot / spam filter. A tweet is dropped if its author trips any of these.
# ---------------------------------------------------------------------------
BOT_RULES = {
    "drop_if_automated_flag": True,          # X's own "Automated" label
    "drop_if_username_matches": r"(?i)(bot|_ia$|_ai$|noticias24|news24)",
    "max_statuses_per_day": 150,             # tweets/day over account lifetime
    "min_account_age_days": 14,
    "drop_if_no_text_after_mentions": True,  # replies that are only @handles / links
    "min_text_chars": 25,
}

# ---------------------------------------------------------------------------
# Volume / cost controls (twitterapi.io: ~$0.15 per 1,000 tweets returned)
# ---------------------------------------------------------------------------
BACKFILL = {
    "months_back": 12,
    "target_new_tweets": 3000,
    "max_pages_per_query_window": 15,   # 20 tweets/page → 300 per query per month
    "max_thread_pages": 4,              # 80 replies per news thread
    "max_threads": 120,
    "hard_cap_tweets_fetched": 12000,   # ≈ $1.80 worst case
}

DAILY = {
    "lookback_hours": 36,               # overlap on purpose; dedupe handles it
    "target_new_tweets": 100,
    "max_pages_per_query": 3,
    "max_thread_pages": 2,
    "max_threads": 15,
    "hard_cap_tweets_fetched": 1000,    # ≈ $0.15 worst case
}

SUPABASE_TABLE = "dc_tweets"
SUPABASE_RUNS_TABLE = "dc_runs"
