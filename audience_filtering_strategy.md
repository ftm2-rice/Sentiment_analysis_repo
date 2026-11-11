# Audience & Data Filtering Strategy (`config.py`)

The scraper **collects** (search query, known-id skip, retweet exclusion, budget cap). The Colab notebook **filters**. Bot, short-text, off-topic and duplicate-text rules below are Colab rules. They are not applied by `scrape.py`. `is_on_topic` is also used by the scraper only to choose which outlet posts are worth expanding into threads.

---

## 1. Core Inclusion Rules (What We Keep)

To enter the dataset, a tweet must strictly relate to **Data Center developments in Argentina**.

### A. Keyword & Search Queries
* **Core Topic Match:** Every tweet must contain at least one primary topic keyword:
  * `"data center"`, `"datacenter"`, `"centro de datos"`, `"centro de cómputo"`
* **Geographic / Contextual Anchors:** Tweets must explicitly connect to Argentina through location, infrastructure, or regulatory anchors:
  * *Locations/Regions:* Argentina, Patagonia, Río Negro, Sierra Grande, Bahía Blanca, Buenos Aires, Córdoba, Mendoza, Neuquén, Vaca Muerta, Añelo.
  * *Policy/Energy:* RIGI, Secretaría de Energía, CAMMESA, ENARSA, Resolução 264.
* **Company Hooks:** Major tech entities (e.g., OpenAI, Microsoft, Google, AWS, Nvidia, Cirion) are queried **only** when paired directly with Argentina anchors and core data center terms to avoid global off-topic chatter.

### B. Proximity Rule for Long Form Content
* For tweets longer than **600 characters** (`LONG_TEXT_CHARS`), the topic term and Argentina anchor must appear within **200 characters** (`PROXIMITY_CHARS`) of each other. This prevents keeping lengthy news digests that mention "data centers" and "Argentina" in completely unrelated paragraphs.

---

## 2. Audience & Segment Classification

We categorize preserved tweets into six target audience segments:

1. **Outlet Posts (`outlet_post`)**:
   * Direct posts authored by predefined press, news networks, trade journals, or official entities (e.g., `@clarincom`, `@LANACION`, `@econojournal`, `@MinEconomia_Ar`).
2. **Auto-Detected Outlets**:
   * Accounts with $\ge 50,000$ followers whose topic post generates $\ge 5$ replies.
3. **News Threads (`news_thread`)**:
   * Multi-tweet informational sequences published by media outlets or journalists.
4. **News Replies (`news_reply`) & Quote Tweets (`news_quote`)**:
   * User interactions directly engaging with an official outlet post.
5. **Verified Opinion (`verified_opinion`)**:
   * Standalone posts from non-outlet verified users with $\ge 2,000$ followers.
6. **General Public (`general_public`)**:
   * Organic posts from everyday standard user accounts passing all topic filters.

---

## 3. Exclusion Rules (What Colab Sets Aside)

The scraper still drops plain retweets (`-filter:nativeretweets`, and `retweeted_tweet` in code) and skips ids already in `dc_tweets`. Everything else is stored. The Colab notebook then sets aside tweets that match the rules below:

### A. False Positives & Irrelevant Context
* **Commodity / Comparison References (`ARGENTINA_FALSE_RE`):**
  * Dropped if "Argentina" is used strictly as a comparison or trade commodity (e.g., *"beef from argentina"*, *"carne argentina"*, *"than argentina"*).
* **Sports & Culture Off-Topic:**
  * Mentions of Messi, World Cup (*mundial*), or sports matches (*vs Argentina*).
* **Standalone Political Noise:**
  * Generic political chatter mentioning Milei, Caputo, or US Stargate without explicit data center context.

### B. Structural & Bot/Spam Filters (`BOT_RULES`)
* **Native Retweets:** Removed by the scraper (`-filter:nativeretweets`, plus any `retweeted_tweet` the API still returns). Not a Colab filter.
* **X Automated Flag:** Dropped if the account carries X's official "Automated" label.
* **Spam Username Patterns:** Handles matching bot patterns (e.g., containing `bot`, `_ia`, `_ai`, `ainew`, `noticias24`, `digest`, `summary`).
* **High Frequency Accounts:** Accounts posting $>150$ tweets per day over their lifetime.
* **Account Age:** Accounts under 14 days old.
* **Empty Content:** Tweets with $< 25$ characters or replies containing only `@handles` and URLs without text.