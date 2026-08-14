# US Stock L0 Pipeline B (Social Media Monitoring) — Design Document

> **A-Share Pipe B Equivalent for US Markets**
> Built on `l0_monitor_v1.py` architecture — extend `PipeBSocialMedia` class
> Date: 2026-06-05

---

## Table of Contents

1. [KOL Watchlist (Categorized)](#1-kol-watchlist)
2. [Data Sources (Accessibility Ratings)](#2-data-sources)
3. [Signal Design (Thresholds & Anomaly Detection)](#3-signal-design)
4. [Integration with Existing L0 Monitor](#4-integration)
5. [Scheduler Extension (US Timezone)](#5-scheduler-extension)
6. [Implementation Roadmap](#6-implementation-roadmap)

---

## 1. KOL Watchlist

### Tier 1: Mega-Cap Movers (CEO/Founder Level)
Tweets from these individuals can single-handedly move stocks 5-20% intraday.

| Handle | Name | Impact Target | Signal Type | Weight |
|--------|------|--------------|-------------|--------|
| @elonmusk | Elon Musk | TSLA, DOGE, SPACs | Direct price mover | 1.0 |
| @realDonaldTrump | Donald Trump | DJT, sector-wide policy | Policy/Regulation | 0.9 |
| @sama | Sam Altman | AI sector (NVDA, MSFT, GOOG) | Sector sentiment | 0.8 |
| @nvidia | Jensen Huang (via Nvidia account) | NVDA, AI semi | Company news | 0.7 |
| @JeffBezos | Jeff Bezos | AMZN, Blue Origin | Rare but heavy | 0.6 |
| @tim_cook | Tim Cook | AAPL | Rare but heavy | 0.6 |
| @finkd | Mark Zuckerberg | META | Rare but heavy | 0.6 |

### Tier 2: FinTwit Real Traders (Highest Signal-to-Noise)
These are practitioners who post real-time trades, setups, and chart analysis. **Not promoters, not shillers.**

| Handle | Name | Specialty | Signal Type |
|--------|------|----------|-------------|
| @markminervini | Mark Minervini | Growth stock, VCP patterns, SEPA strategy | Specific ticker calls |
| @PeterLBrandt | Peter L. Brandt | Classic chart patterns, commodity/FX | Pattern-based alerts |
| @RedDogT3 | Scott Redler | Tech levels, indices, swing trading | Daily technicals |
| @LindaRaschke | Linda Raschke | Short-term price action | Intraday signals |
| @canucky | Canucky | Mid-cap technicals | Ticker-specific |
| @jessefelder | Jesse Felder | Contrarian macro, bearish deep dives | Thematic alerts |
| @John_Hempton | John Hempton | Short selling, forensic accounting | Fraud/short alerts |
| @CitronResearch | Citron Research | Short seller reports | Short thesis alerts |
| @KobeissiLetter | Kobeissi Letter | Real-time market commentary | Broad market |

### Tier 3: Institutional Voices (Portfolio Movers)
These investors' public statements can move sectors or individual stocks.

| Handle | Name | Firm | Signal Type |
|--------|------|------|-------------|
| @BillAckman | Bill Ackman | Pershing Square | Activist positions, macro |
| @CassandraBS | Michael Burry | Scion Asset Management | Short bets, contrarian |
| @Carl_C_Icahn | Carl Icahn | Icahn Enterprises | Activist campaigns |
| @ARKInvest | Cathie Wood | ARK Invest | Thematic tech, biotech |
| @TruthGundlach | Jeffrey Gundlach | DoubleLine | Fixed income, macro |
| @RayDalio | Ray Dalio | Bridgewater | Economic cycles, macro |
| @chamath | Chamath Palihapitiya | Social Capital | SPACs, tech |

### Tier 4: News Breakers (Market-Moving Journalism)
These accounts break news before it hits wires.

| Handle | Name | Specialty | Signal Type |
|--------|------|----------|-------------|
| @DeItaone | Walter Bloomberg (live squad) | M&A, corporate events | Breaking M&A |
| @SchaeferStreet | Carl Quintanilla | CNBC anchor | Market-moving interviews |
| @benzinga | Benzinga News Wire | Real-time news feed | Breaking news |
| @WSJ | Wall Street Journal | Corporate news | Investigative |
| @FT | Financial Times | Global finance | Macro news |
| @zerohedge | Zero Hedge | Contrarian macro | Pre-market signals |

### Tier 5: Reddit / Social Community Influencers

| Name / Account | Platform | Notability | Signal Type |
|---------------|----------|-----------|-------------|
| DeepFuckingValue / TheRoaringKitty | Reddit/X | Keith Gill, GME legend (returned 2024, 2026) | Meme stock ignition |
| Quiver Quantitative (aggregated) | Reddit (WSB) | Algorithmic tracking of r/WallStreetBets | Trending ticker detection |
| r/wallstreetbets top weekly performers | Reddit | Weekly momentum posts | Momentum tickers |

### Tier 6: KOL Dynamic Discovery Pool
Same 3-confirmation mechanism as A-share system applied to:
- New FinTwit accounts with rapid follower growth (>20% follower growth/month)
- Accounts consistently mentioned alongside significant price moves
- Seeking Alpha authors with >90% rating accuracy for 3+ months

---

## 2. Data Sources

### Source Matrix

| # | Source | Data Available | Access Method | Rating | Cost | Latency | Notes |
|---|--------|---------------|---------------|--------|------|---------|-------|
| 1 | **Reddit** (r/WSB, r/stocks, r/investing) | Ticker mentions, sentiment, post count, cross-subreddit penetration | PRAW (Python Reddit API Wrapper) | ✅ **FREE** | $0 | Real-time | Rate limit: 60 req/min. Pushshift for historical |
| 2 | **StockTwits** | Ticker-specific message streams, bullish/bearish tags, volume | StockTwits API (free tier) | ✅ **FREE** | $0 | 1-2 min | Best signal-to-noise ratio among free sources. Native cashtag structure |
| 3 | **X/Twitter FinTwit** | KOL tweets, retweets, engagement metrics | Nitter self-hosted (proxy) | 🔶 **PROXY-NEEDED** | $0 (hosting) | 5-15 min | Twitter API v2 Basic = $100/mo (heavy). Nitter works but unstable. X API $200/mo for read |
| 4 | **Seeking Alpha** | Articles, author ratings, quant ratings, earnings transcripts | AlphaFactoryX MCP (free) or SKAlpha RapidAPI (paid) | 🔶 **API-NEEDED** | $0-$30/mo | 1-6h | No official free API. Direct scraping blocked by CAPTCHA |
| 5 | **Benzinga Pro** | News wire, earnings alerts, analyst ratings | Benzinga API (paid) | 🔴 **API-NEEDED** | ~$150/mo | Real-time | Best real-time news wire. No free tier |
| 6 | **Quiver Quantitative** | Aggregated Reddit + StockTwits sentiment | `quiverquant` Python package | ✅ **FREE-LIMITED** | $0-$10/mo | Daily | Free tier covers basics. Premium $25/mo for API. Python package open source |
| 7 | **Yahoo Finance** (yfinance) | Price data, fundamentals, news headlines | yfinance Python library | ✅ **FREE** | $0 | 15-min delay | Real-time via paid Yahoo Finance subscription |
| 8 | **SEC EDGAR** | 13F filings, Form 4 insider trades, 8-K events | SEC EDGAR bulk data | ✅ **FREE** | $0 | 1-2 days | Form 4 = US equivalent of 龙虎榜 insider signals |
| 9 | **Finnhub** | News sentiment, SEC filings, earnings transcripts | Finnhub API (free tier) | ✅ **FREE-LIMITED** | $0-$20/mo | Real-time | Free tier: 60 API calls/min. Good for news sentiment |
| 10 | **Alpha Vantage** | News & sentiment API | Alpha Vantage API | ✅ **FREE-LIMITED** | $0 | 15+ min | Free: 25 premium calls/day, 500 standard/day. MCP server available |
| 11 | **Unusual Whales** | Options flow, dark pool, greek exposure | Web dashboard (free limited) + Enterprise API | 🔴 **API-NEEDED** | Enterprise only | Real-time | MCP server available (npm: unusual-whales-mcp). Free web: limited screens |
| 12 | **Finviz** | Stock screener, insider trading, news | Finviz web scraping | ✅ **FREE** | $0 | 15-min delay | Finviz Elite API = $299/yr. Free web OK for manual reference |
| 13 | **Google Trends** | Search interest for ticker symbols | `pytrends` unofficial API | ✅ **FREE** | $0 | Daily | Useful for retail attention spikes |
| 14 | **SEC Form 4 Tracker** | Insider buying/selling | SEC EDGAR direct query | ✅ **FREE** | $0 | 1-day lag | High signal: insider buys >$1M → strong bullish signal |
| 15 | **CNBC / Bloomberg headlines** | Breaking news | Web scraping / RSS | ✅ **FREE** | $0 | Real-time | RSS feeds available for free |

### Accessibility Tier Summary

| Tier | Sources | Strategy |
|------|---------|----------|
| **T1: Always-on FREE** | Reddit (PRAW), StockTwits API, Yahoo Finance (yfinance), SEC EDGAR, Finviz, Google Trends | Core always-running pipeline. No auth issues. |
| **T2: Free-limited** | Quiver Quantitative, Finnhub, Alpha Vantage | Good supplementary sources. Rate-limit aware scheduling. |
| **T3: Proxy-needed** | X/Twitter via Nitter, Finviz (if heavy scraping) | Requires proxy infrastructure. Self-hosted Nitter instance. |
| **T4: API-needed (paid)** | Twitter API v2, Seeking Alpha (SKAlpha), Benzinga, Unusual Whales | Budget-dependent. Benzinga highest priority for real-time news. |

### Recommended Tier-1 Pipeline (Zero Cost)

```
PRAW (Reddit) ──┬──> Ticker mention count (Z-score vs 20d)
                 └──> VADER sentiment scoring

StockTwits API ──┬──> Message volume (ratio vs 20d)
                  └──> BullBear ratio shift

Yahoo Finance ─────> Trigger price confirmation (2%+ move)
(free)

SEC EDGAR ─────────> Insider transaction monitoring (Form 4)
(free bulk)
```

---

## 3. Signal Design

### 3.1 Reddit Signal (r/WallStreetBets + r/stocks + r/investing)

**Detection Method:**
```python
# Using PRAW to pull hot/new posts from target subreddits
# Extract ticker mentions via regex: $[A-Z]{1,5} or [A-Z]{1,5} in context
# Aggregate by ticker per subreddit per scan interval
```

**Thresholds:**

| Signal | Metric | Threshold | Score Contribution | Notes |
|--------|--------|-----------|-------------------|-------|
| **Mention Spike** | Ticker mention count Z-score vs 20d rolling mean | > 3σ | 0.30 | A股 equivalent was 2.5σ; raised for US due to higher noise |
| **Sustained Spike** | > 2σ for 3 consecutive scans | > 2σ × 3 scans | 0.15 | Persistence bonus |
| **Sentiment Flip** | VADER compound shift from negative (< -0.3) to positive (> 0.3) in 2h | Δ > 0.6 | 0.10 | Bear-to-bull polarity reversal |
| **Cross-Subreddit** | Same ticker appears in ≥ 2 of [WSB, stocks, investing] within 12h | ≥ 2 subreddits | 0.15 | Organic spread (vs manipulated) |
| **Meme Detection** | Post upvote ratio > 0.9 AND comment count > 5x median | Compound | 0.10 | Reddit algorithm picking up |
| **WSB Top 10 Entry** | Ticker enters WSB's daily top 10 tickers | Entry event | 0.05 | Late signal but confirmation |

**Reddit Score Formula:**
```python
reddit_score = min(
    mention_zscore * 0.10 +      # Each σ above mean = 0.10
    sustained_bonus +             # 0.15 if persistent
    sentiment_flip +              # 0.10 if flip detected
    cross_subreddit_bonus +       # 0.15 if cross-subreddit
    meme_detection +              # 0.10 if organic viral
    top10_entry,                  # 0.05 if WSB top 10
    0.50                          # Cap at 0.50
)
```

### 3.2 StockTwits Signal

**Detection Method:**
```python
# GET https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json
# Returns messages with: body, sentiment (Bullish/Bearish), user, created_at
# Aggregate: count by ticker, compute Bullish% = bullish / (bullish + bearish)
```

**Thresholds:**

| Signal | Metric | Threshold | Score Contribution | Notes |
|--------|--------|-----------|-------------------|-------|
| **Volume Spike** | Message count / 20d avg daily volume | > 5x | 0.25 | A股 community_heat_ratio was 3.0; raised to 5x for StockTwits |
| **Sentiment Deviation** | Current bullish% Z-score vs 20d mean | > 2σ | 0.15 | Statistically significant sentiment shift |
| **Extreme Sentiment** | Bullish% > 80% or < 20% | Raw > 80% | 0.10 | Absolute extreme, not relative |
| **Rapid Flip** | Bullish% changes by > 30pp in 4h | Δ > 30pp | 0.10 | Behavior change |
| **Influencer Post** | Message from user with > 10k followers | Any | 0.05 | Weighted add |

**StockTwits Score Formula:**
```python
st_score = min(
    volume_ratio * 0.05 +          # Each 1x above baseline = 0.05, cap at 5x
    sentiment_zscore * 0.075 +     # Each σ = 0.075
    extreme_sentiment +            # 0.10 if > 80% or < 20%
    rapid_flip +                   # 0.10 if Δ > 30pp
    influencer_post,               # 0.05 if influencer post
    0.40                           # Cap at 0.40
)
```

### 3.3 X/Twitter FinTwit Signal

**Detection Method:**
```python
# Via Nitter self-hosted instance (proxy) or Twitter API v2
# Track specific KOL accounts from watchlist
# Search: "from:{handle} {ticker}" AND "{ticker}" across FinTwit
# Retweet/engagement velocity tracking
```

**Thresholds:**

| Signal | Metric | Threshold | Score Contribution | Notes |
|--------|--------|-----------|-------------------|-------|
| **Tier1 Mention** | Elon/Trump/Sama tweet about ticker | 1 mention | 0.40 | Direct market mover; scores higher than any other single signal |
| **Tier2 Mention** | FinTwit trader mentions ticker | 1 mention | 0.25 | Weight adjusted by KOL track record |
| **Tier3 Mention** | Institutional voice mentions | 1 mention | 0.20 | Adjusted by Bayesian weight |
| **Cross-KOL Confirmation** | ≥ 2 distinct KOLs mention same ticker within 4h | ≥ 2 KOLs | +0.15 bonus | Highest-conviction signal |
| **Viral Engagement** | Tweet about ticker gets > 5x KOL's avg engagement | Ratio > 5x | 0.10 | Organic interest confirmation |
| **New KOL Discovery** | Account with < 50k followers gets > 100k impressions on ticker post | Any | 0.05 | Early detection |

**Twitter Score Formula:**
```python
twitter_score = min(
    max_kol_score +                # Highest single KOL mention score
    cross_kol_bonus +              # +0.15 if >1 KOL
    viral_engagement,              # +0.10 if viral
    0.50                           # Cap at 0.50
)
```

### 3.4 Seeking Alpha Signal

**Detection Method:**
```python
# Via AlphaFactoryX MCP (free) or SKAlpha API
# Track: article publication rate, author rating, quant rating
# Compare: author recommendation vs quant rating divergence
```

**Thresholds:**

| Signal | Metric | Threshold | Score Contribution | Notes |
|--------|--------|-----------|-------------------|-------|
| **Article Velocity** | ≥ 3 articles on same ticker within 24h | ≥ 3 articles | 0.15 | Unusual research focus |
| **Top Author Move** | Article by top-decile-rated author | Any | 0.15 | High-authority signal |
| **Rating Divergence** | Author says Buy but SA Quant says Sell (or vice versa) | Divergence exists | 0.10 | Contrarian indicator |
| **Premium Content** | Exclusive analysis published (not just news recap) | Deep dive | 0.05 | Higher quality |
| **Bearish Thesis** | Strong sell / short thesis published | Any | 0.10 | Less common, more impactful |

**Seeking Alpha Score Formula:**
```python
sa_score = min(
    article_velocity_bonus +       # 0.15 if ≥3
    top_author_bonus +             # 0.15 if top author
    divergence_bonus +             # 0.10 if divergence
    bearish_thesis,               # 0.10 if bearish
    0.30                           # Cap at 0.30
)
```

### 3.5 Benzinga News Wire Signal

**Detection Method:**
```python
# Via Benzinga API (paid) — or Finnhub free tier news
# Track: news velocity per ticker, event category, pre-market timing
# Categories: earnings, analyst, M&A, regulation, SEC filing
```

**Thresholds:**

| Signal | Metric | Threshold | Score Contribution | Notes |
|--------|--------|-----------|-------------------|-------|
| **News Velocity** | > 3 news items/hour for ticker | > 3/h | 0.15 | Unusual news concentration |
| **Pre-Market News** | Any material news before 9:30 AM ET | Any | 0.10 | Market-moving potential |
| **After-Hours News** | Material news after 4:00 PM ET | Any | 0.10 | Gap risk |
| **M&A / Activist** | Merger, acquisition, activist filing | Any | 0.15 | Highest impact category |
| **Analyst Move** | Major upgrade/downgrade (multiple firms) | ≥ 2 firms | 0.10 | Consensus shift |
| **Earnings Surprise** | Pre-announcement or whisper number | Any | 0.10 | Early earnings signal |

**Benzinga Score Formula:**
```python
benzinga_score = min(
    news_velocity_score +          # 0.15 if > 3/h
    timing_bonus +                 # 0.10 if pre/after hours
    m_and_a_bonus +                # 0.15 if M&A
    analyst_bonus +                # 0.10 if multi-firm analyst
    earnings_bonus,               # 0.10 if earnings
    0.40                           # Cap at 0.40
)
```

### 3.6 US-Specific Supplementary Signals

| Source | Signal | Metric | Threshold | Score |
|--------|--------|--------|-----------|-------|
| **SEC Form 4** | Insider purchase > $1M | Dollar value | > $1M | 0.15 |
| **SEC Form 4** | Multiple insiders buying (≥ 3) | Count | ≥ 3 insiders | 0.10 |
| **Options Flow (Unusual Whales)** | Unusual call volume | Volume/avg | > 5x | 0.10 |
| **GEX (Gamma Exposure)** | Large negative gamma → volatility | Gamma level | Bottom 5% | 0.05 |
| **Google Trends** | Search term spike for ticker | Trend Z-score | > 3σ | 0.05 |

### 3.7 US Pipe B Composite Score

```python
def score_us(code_name: str) -> Dict:
    """
    US equity Pipe B composite score.
    Sources combined with diminishing marginal weight.
    """
    reddit_data = check_reddit(code_name)        # Max 0.50
    stocktwits_data = check_stocktwits(code_name) # Max 0.40
    twitter_data = check_kol_mentions(code_name)  # Max 0.50
    seeking_alpha_data = check_seeking_alpha(code_name)  # Max 0.30
    benzinga_data = check_benzinga(code_name)     # Max 0.40

    # Diminishing marginal returns: each additional source adds less
    scores = sorted([
        reddit_data["score"],
        stocktwits_data["score"],
        twitter_data["score"],
        seeking_alpha_data["score"],
        benzinga_data["score"],
    ], reverse=True)

    weights = [0.35, 0.25, 0.20, 0.15, 0.05]  # Diminishing weights
    composite = sum(s * w for s, w in zip(scores, weights))

    verdict = "normal"
    if composite > 0.4:
        verdict = "watch"
    if composite > 0.65:
        verdict = "alert"

    return {
        "pipe_score": composite,
        "signals": [reddit_data, stocktwits_data, twitter_data, seeking_alpha_data, benzinga_data],
        "verdict": verdict,
    }
```

### 3.8 Config Section (New US Block)

```python
CONFIG = {
    # ... existing A-share config ...

    # US stock Pipe B config
    "us_pipe_b": {
        "kol_watchlist": [           # see Section 1
            "elonmusk",
            "realDonaldTrump",
            "sama",
            "markminervini",
            "PeterLBrandt",
            "RedDogT3",
            "BillAckman",
            "CassandraBS",
            "CitronResearch",
            "DeItaone",
            "KobeissiLetter",
            "zerohedge",
        ],
        "us_keywords": [             # US story-driven themes
            "AI", "AGI", "LLM", "GPU",
            "quantum", "nuclear", "fusion",
            "GLP-1", "weight loss", "obesity",
            "EV", "autonomous", "robotaxi",
            "semiconductor", "CHIPS Act",
            "defense", "aerospace",
            "cybersecurity", "SaaS",
            "biotech", "gene editing", "CRISPR",
            "meme stock", "short squeeze",
            "Bitcoin", "crypto", "blockchain",
        ],
        "subreddits": ["wallstreetbets", "stocks", "investing", "smallstreetbets"],
        "reddit_zscore": 3.0,              # A股同指标2.5，美股噪音较高
        "stocktwits_volume_ratio": 5.0,    # 社区讨论量阈值
        "stocktwits_sentiment_zscore": 2.0,
        "twitter_cross_kol_confirm": 2,    # 跨KOL确认数
        "sa_article_velocity": 3,          # 文章发布频率
        "benzinga_news_per_hour": 3,
        "insider_purchase_min_usd": 1000000,  # SEC Form 4
        "lookback_days": 20,
    },

    # US-specific fusion weights
    "us_fusion": {
        "pipe_a_weight": 0.40,       # US主流资金流权重重于A股
        "pipe_b_weight": 0.60,       # 社媒对美股的影响大于A股
        "single_pipe_trigger": 0.55, # 略低于A股0.6（美股更灵敏）
        "dual_pipe_watch": 0.35,
        "dual_pipe_critical": 0.65,
        "price_confirm_pct": 2.5,    # 美股波动率更高
    },
}
```

### 3.9 Threshold Comparison: A-Share vs US

| Metric | A-Share (Current) | US (Proposed) | Rationale |
|--------|-------------------|---------------|-----------|
| Search heat Z-score | 2.5 | 3.0 (Reddit) | US social media has higher noise floor |
| Community heat ratio | 3.0x | 5.0x (StockTwits) | StockTwits is more ticker-dense than Chinese forums |
| KOL mention trigger | 1 | 1 (but scored by tier) | Same base trigger; US scores weighted by KOL credibility |
| Price confirm % | 2.0% | 2.5% | US stocks have higher daily volatility |
| Pipe A weight | 0.50 | 0.40 | US funds flow less concentrated in retail channels |
| Pipe B weight | 0.50 | 0.60 | US social media more impactful on retail-driven moves |
| Dual-pipe critical min | 0.60 | 0.65 | Higher bar for CRITICAL given higher noise |
| Scan interval | 5 min | 5 min | Same; US market hours overlap with Beijing night |
| New KOL confirmation | 3 hits | 3 hits | Same mechanism |

---

## 4. Integration with Existing L0 Monitor

### 4.1 Architecture: Multi-Market Extendable Pipe B

The existing `PipeBSocialMedia` class becomes a base class with two subclasses:

```
PipeBSocialMedia (base, l0_monitor_v1.py)
├── PipeBSocialMediaCN (A-Share, existing logic)
│   ├── sources: Zhihu, Xueqiu, Weibo, Baidu search
│   ├── KOLs: aleabitoreddit, elonmusk, jimcramer, arkinvest
│   └── keywords: Chinese story keywords
│
└── PipeBSocialMediaUS (US, new)
    ├── sources: Reddit, StockTwits, Twitter FinTwit, Seeking Alpha, Benzinga
    ├── KOLs: FinTwit + CEO + Institutional (12+ accounts)
    └── keywords: US story keywords
```

### 4.2 US PipeB Class Design

```python
class PipeBSocialMediaUS(PipeBSocialMedia):
    """US stock social media monitoring — extends base PipeBSocialMedia."""

    def __init__(self, config):
        # Extract US-specific config
        us_cfg = config.get("us_pipe_b", {})
        super().__init__(config)  # Inherits KOL discovery, Bayesian weighting
        self.us_cfg = us_cfg
        self.kols = us_cfg.get("kol_watchlist", [])
        self.keywords = us_cfg.get("us_keywords", [])
        self.subreddits = us_cfg.get("subreddits", ["wallstreetbets"])
        self.history = {
            "reddit": {},     # {ticker: deque of daily mention counts}
            "stocktwits": {}, # {ticker: deque of daily message counts}
            "sentiment": {},  # {ticker: deque of daily sentiment scores}
        }

    # ── Source-specific check methods ──

    def check_reddit(self, ticker: str) -> Dict:
        """
        Check Reddit for ticker mentions across target subreddits.
        Uses PRAW (free).
        """
        # Example implementation:
        # import praw
        # reddit = praw.Reddit(client_id=..., client_secret=..., user_agent=...)
        # mentions = 0
        # for sub in self.subreddits:
        #     subreddit = reddit.subreddit(sub)
        #     for post in subreddit.hot(limit=50):
        #         if ticker in post.title or ticker in post.selftext:
        #             mentions += 1
        # Compute Z-score vs 20d rolling mean
        zscore = self.compute_zscore(self.history["reddit"].get(ticker, []), mentions)
        return {"mentions": mentions, "zscore": zscore, "score": min(zscore * 0.10, 0.50)}

    def check_stocktwits(self, ticker: str) -> Dict:
        """
        Check StockTwits for ticker message volume and sentiment.
        Uses free StockTwits API.
        """
        # Example implementation:
        # import requests
        # resp = requests.get(f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json")
        # messages = resp.json()["messages"]
        # bullish = sum(1 for m in messages if m["entities"]["sentiment"]["basic"] == "Bullish")
        # bearish = sum(1 for m in messages if m["entities"]["sentiment"]["basic"] == "Bearish")
        # total = len(messages)
        # bullish_pct = bullish / total if total > 0 else 0.5
        volume_ratio = 0  # self.history check
        sentiment_dev = 0  # Z-score calculation
        return {"volume_ratio": volume_ratio, "sentiment": 0, "score": 0}

    def check_seeking_alpha(self, ticker: str) -> Dict:
        """
        Check Seeking Alpha via AlphaFactoryX MCP or Finnhub.
        """
        # Via MCP: call AlphaFactoryX sa_article or sa_search endpoint
        return {"article_count": 0, "top_author": False, "score": 0}

    def check_benzinga(self, ticker: str) -> Dict:
        """
        Check Benzinga news wire via Finnhub free API (proxy).
        Finnhub free tier: 60 calls/min.
        """
        # import finnhub
        # finnhub_client = finnhub.Client(api_key="...")
        # news = finnhub_client.company_news(ticker, _from=..., to=...)
        return {"news_per_hour": 0, "categories": [], "score": 0}

    def check_insider_trades(self, ticker: str) -> Dict:
        """
        Check SEC Form 4 insider transactions via Finviz or SEC EDGAR.
        Free bulk data from SEC.gov.
        """
        return {"insider_buys": 0, "total_value": 0, "n_insiders": 0, "score": 0}

    # ── Override base score() for US composite ──

    def score(self, ticker: str) -> Dict:
        """
        US Pipe B composite score — overrides base class.
        """
        reddit = self.check_reddit(ticker)
        st = self.check_stocktwits(ticker)
        kol = self.check_kol_mentions(ticker)
        sa = self.check_seeking_alpha(ticker)
        benzinga = self.check_benzinga(ticker)
        insider = self.check_insider_trades(ticker)

        # Diminishing marginal weights
        all_scores = sorted([
            (reddit["score"], "reddit"),
            (st["score"], "stocktwits"),
            (kol.get("score", 0), "twitter"),
            (sa["score"], "seekingalpha"),
            (benzinga["score"], "benzinga"),
        ], key=lambda x: x[0], reverse=True)

        weights = [0.35, 0.25, 0.20, 0.15, 0.05]
        composite = sum(s[0] * w for s, w in zip(all_scores, weights))

        # Insider bonus (independent of main composite)
        if insider["score"] > 0:
            composite = min(composite + 0.10, 1.0)

        verdict = "normal"
        if composite > 0.4:
            verdict = "watch"
        if composite > 0.65:
            verdict = "alert"

        return {
            "pipe_score": composite,
            "signals": [reddit, st, kol, sa, benzinga, insider],
            "verdict": verdict,
        }
```

### 4.3 L0FusionEngine Extension

```python
class L0FusionEngine:
    """Dual-pipe fusion engine — supports both A-share and US markets."""

    def __init__(self, config, pipe_a, pipe_b_cn=None, pipe_b_us=None):
        self.cfg = config["fusion"]
        self.us_cfg = config.get("us_fusion", self.cfg)  # Fallback to CN
        self.pipe_a = pipe_a
        self.pipe_b_cn = pipe_b_cn or pipe_b  # A-share Pipe B
        self.pipe_b_us = pipe_b_us            # US Pipe B
        # ... rest of init ...

    def scan_single_stock_us(self, ticker: str) -> Dict:
        """
        Scan single US stock — uses US Pipe B + US fusion weights.
        """
        pipe_b = self.pipe_b_us
        result_b = pipe_b.score(ticker)

        score_b = result_b["pipe_score"]

        # US Pipe A (to be implemented as PipeAMainForceUS):
        score_a = 0  # placeholder until US Pipe A is built

        fusion_cfg = self.us_cfg
        w_a = fusion_cfg["pipe_a_weight"]
        w_b = fusion_cfg["pipe_b_weight"]

        # Same fusion logic as base, with US thresholds:
        add_term = score_a * w_a + score_b * w_b
        mul_term = score_a * score_b
        fusion_score = add_term * 0.6 + mul_term * 0.4

        alert_level = self.classify_alert_us(score_a, score_b, fusion_score, price_confirmed)

        return {
            "ticker": ticker,
            "market": "US",
            "timestamp": datetime.now().isoformat(),
            "pipe_b": result_b,
            "fusion_score": fusion_score,
            "alert_level": alert_level,
            # ...
        }
```

### 4.4 Watchlist Integration

```python
class WatchlistManager:
    """Dual-market watchlist manager."""

    WATCHLIST_PATHS = {
        "CN": os.path.expanduser("~/hermes_output/quant/l0_watchlist_cn.json"),
        "US": os.path.expanduser("~/hermes_output/quant/l0_watchlist_us.json"),
    }

    @staticmethod
    def build_watchlist_us() -> Dict:
        """
        US watchlist: focus on:
        - High retail ownership stocks (SPY/QQQ components with high retail %)
        - Meme-able names (high short interest, high beta)
        - Pre-market gappers
        - Recent IPO / SPAC
        """
        return {
            "core_pool": [
                {"ticker": "TSLA", "name": "Tesla", "tags": ["EV", "AI", "meme"], "risk_score": 0.85},
                {"ticker": "NVDA", "name": "NVIDIA", "tags": ["AI", "semiconductor", "GPU"], "risk_score": 0.65},
                {"ticker": "GME", "name": "GameStop", "tags": ["meme", "retail"], "risk_score": 0.95},
                {"ticker": "AMC", "name": "AMC Entertainment", "tags": ["meme", "retail"], "risk_score": 0.90},
                {"ticker": "AAPL", "name": "Apple", "tags": ["tech", "mega-cap"], "risk_score": 0.20},
                {"ticker": "MSTR", "name": "MicroStrategy", "tags": ["Bitcoin", "volatility"], "risk_score": 0.80},
                {"ticker": "PLTR", "name": "Palantir", "tags": ["AI", "defense", "meme"], "risk_score": 0.75},
                {"ticker": "DJT", "name": "Trump Media", "tags": ["meme", "political"], "risk_score": 0.95},
                {"ticker": "RKLB", "name": "Rocket Lab", "tags": ["space", "growth"], "risk_score": 0.70},
                {"ticker": "SMCI", "name": "Super Micro", "tags": ["AI", "server", "volatility"], "risk_score": 0.80},
                # Dynamic: pre-market gappers → auto-add daily
            ],
            "auto_added": [],  # Auto-added from pre-market scanning
            "last_updated": datetime.now().isoformat(),
        }
```

---

## 5. Scheduler Extension

### 5.1 Market Time Mapping (Beijing Time)

```
US EDT (March-Nov): UTC-4. Beijing: UTC+8. Diff = +12h.
US EST (Nov-March): UTC-5. Beijing: UTC+8. Diff = +13h.

Current (June 2026): EDT. Beijing = US + 12h.

US Pre-market:  4:00-9:30 ET   = 16:00-21:30 Beijing
US Open:        9:30-16:00 ET  = 21:30-04:00 Beijing
US After-hours: 16:00-20:00 ET = 04:00-08:00 Beijing

A-Share Open:   9:30-15:00 Beijing = 21:30-03:00 ET (US closed)
```

US and A-Share trading hours are almost perfectly complementary. The L0 system can handle both markets sequentially.

### 5.2 Dual-Market L0 Scheduler

```python
class L0Scheduler:
    """Dual-market L0 scheduler — sequential coverage."""

    SCHEDULE = {
        # Beijing Time
        # ============
        # 08:00-09:00  US after-hours review + A-Share pre-market
        "cn_pre_market": {
            "time": "08:00-09:15",
            "market": "CN",
            "priority": "pipe_b",      # Check overnight US social media + CN morning news
            "output": "L0_PREMARKET_CN",
        },
        # 09:30-15:00  A-Share regular hours
        "cn_regular": {
            "time": "09:30-11:30,13:00-15:00",
            "market": "CN",
            "priority": "both",
            "interval": "5min",
            "output": "L0_INTRADAY_CN",
        },
        # 15:00-15:30  A-Share post-market (龙虎榜)
        "cn_post_market": {
            "time": "15:00-15:30",
            "market": "CN",
            "priority": "pipe_a",
            "output": "L0_POSTMARKET_CN",
        },
        # 16:00-21:00  US pre-market scanning (news, KOLs, pre-market movers)
        "us_pre_market": {
            "time": "16:00-21:00",
            "market": "US",
            "priority": "pipe_b",      # Social media dominant pre-market
            "interval": "15min",       # Lower frequency before open
            "output": "L0_PREMARKET_US",  # Pre-market gapper list
        },
        # 21:00-21:30  US market open prep
        "us_open_prep": {
            "time": "21:00-21:30",
            "market": "US",
            "priority": "both",
            "output": "L0_OPENING_US",  # Initial watch list
        },
        # 21:30-04:00  US regular hours (high frequency)
        "us_regular": {
            "time": "21:30-04:00",
            "market": "US",
            "priority": "both",
            "interval": "5min",
            "output": "L0_INTRADAY_US",
        },
        # 04:00-08:00  US after-hours + daily aggregation
        "us_post_market": {
            "time": "04:00-08:00",
            "market": "US",
            "priority": "pipe_b",     # After-hours social media recap
            "output": "L0_DAILY_US",   # US daily alert summary
        },
        # Weekend
        "weekend": {
            "time": "Saturday",
            "market": "both",
            "action": "refresh_watchlist_and_kol",
        },
    }
```

### 5.3 Key Scheduling Design Decisions

| Decision | Rationale |
|----------|-----------|
| **US Pipe B runs 16:00-08:00 Beijing** | Covers full US trading cycle (pre-market → regular → after-hours) |
| **US pre-market: 15min interval** | Lower frequency; no live pricing, mostly news scanning |
| **US regular: 5min interval** | Same as A-Share; live monitoring aligned with 5-min candle |
| **A-Share runs 08:00-15:30 Beijing** | Covers A-Share full cycle |
| **No true overlap** | US and A-Share hours are complementary (only 16:00-21:00 is A-Share closed, US pre) |
| **Single machine can handle both** | Sequential scheduling; no parallelism needed for different markets |

---

## 6. Implementation Roadmap

### Phase 1 (Week 1): Core US Pipe B — Free Sources Only

**Build priority:**
1. `PipeBSocialMediaUS` class (subclass of existing `PipeBSocialMedia`)
2. **Reddit source** (PRAW) — ticker mention extraction + Z-score
3. **StockTwits source** (free API) — message volume + sentiment
4. **KOL mention** (Nitter proxy for X/Twitter) — KOL tweet detection
5. **Yahoo Finance** — price confirmation (2.5% trigger)
6. US config block + `CONFIG["us_pipe_b"]` + `CONFIG["us_fusion"]`

**Deliverable:** Working US Pipe B with 3 free sources + KOL check.
**Cost:** $0/month

### Phase 2 (Week 2): Enhanced Sources + Threshold Calibration

**Build priority:**
1. **Finnhub integration** (free tier) — news sentiment, SEC filings
2. **SEC EDGAR** Form 4 insider trade monitoring
3. **Quiver Quantitative** — aggregated Reddit/StockTwits sentiment
4. **Seeking Alpha** via AlphaFactoryX MCP or Finnhub
5. **Nitter self-hosted setup** for robust Twitter access
6. Threshold calibration: backtest on 3 months of US data
7. Multi-market L0 Scheduler

**Deliverable:** 6+ US data sources, calibrated thresholds, dual-market scheduling.
**Cost:** $0-$20/month (if Quiver Premium)

### Phase 3 (Week 3): Paid Sources + US Pipe A

**Build priority:**
1. **Benzinga API** ($150/mo) — real-time news wire
2. **Twitter API v2 Basic** ($100/mo) — reliable KOL tracking
3. **US Pipe A** (options flow + dark pool + SEC filings)
4. **Unusual Whales MCP** — options flow signal
5. Fusion weight backtesting: compare with L3 US quant signals
6. Automated pre-market gapper detection

**Deliverable:** Full US L0 pipeline (Pipe A + Pipe B), paid sources enabled.
**Cost:** ~$250-$300/month (optional, Phase 2 can be free)

### Cost vs Signal Quality Trade-off

| Tier | Monthly Cost | Sources | Expected Signal Quality |
|------|-------------|---------|------------------------|
| Free | $0 | Reddit + StockTwits + Yahoo + SEC + Nitter | Adequate for meme stock detection |
| Budget | $10-30/mo | + Quiver Premium + Finnhub Pro | Good for trend detection |
| Full | $250-300/mo | + Benzinga + Twitter API + Unusual Whales | Best for real-time alerts |

---

## Appendix A: Key Differences Summary

| Dimension | A-Share (Current) | US Stock (Proposed) |
|-----------|-------------------|---------------------|
| Primary community | Xueqiu (雪球), East Money Guba | Reddit (WSB), StockTwits |
| KOL platform | Zhihu, X (Chinese FinTwit) | X/Twitter (FinTwit), Twitter |
| News source | Baidu search (WebSearch) | Benzinga, Finnhub, SEC EDGAR |
| Regulatory signal | 龙虎榜 (Billboard) | SEC Form 4 (Insider trades) |
| Options signal | N/A (limited options) | Options flow (Unusual Whales) |
| Story keywords | Chinese market themes | US market themes |
| Trading hours (Beijing) | 09:30-15:00 (day) | 21:30-04:00 (night) |
| Noise level | Moderate | Higher (5+ social sources) |
| KOL discovery | WebSearch + zhihu API | Nitter + StockTwits trending |
| Best free source | WebSearch heat | StockTwits API (native sentiment) |
| Recommended paid source | zhihu cookie | Benzinga or Finnhub Pro |

## Appendix B: Quick-Start US Watchlist (Core 15)

| Ticker | Name | Category | Why Monitor |
|--------|------|----------|-------------|
| TSLA | Tesla | Core | Musk tweet sensitivity, meme, EV leader |
| NVDA | NVIDIA | Core | AI bellwether, retail obsession |
| GME | GameStop | Meme | WSB poster child, DFV connection |
| AMC | AMC Entertainment | Meme | WSB legacy, retail favorite |
| MSTR | MicroStrategy | Bitcoin | BTC proxy, high vol |
| PLTR | Palantir | Growth | AI + defense, retail darling |
| DJT | Trump Media | Political | Trump correlation |
| SMCI | Super Micro | Growth | AI server, volatile |
| RKLB | Rocket Lab | Space | Retail space play |
| HOOD | Robinhood | Fintech | Retail psyche proxy |
| COIN | Coinbase | Crypto | Crypto market proxy |
| AAPL | Apple | Mega-cap | Broad market health |
| SOFI | SoFi | Fintech | Retail adoption |
| RDDT | Reddit | Social Media | IPO, Reddit IPO sentiment |
| SAVA | Cassava Sciences | Biotech | Binary event, short squeeze history |
