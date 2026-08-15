#!/usr/bin/env python3
"""Read-only Polymarket sentiment radar for Telegram.

Read-only public market scan. No wallet, no private key, no trading.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import warnings
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path
from typing import Any, Callable

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
STATE_DIR = ROOT / "state"
OUTBOX_DIR = ROOT / "outbox"
HISTORY_PATH = STATE_DIR / "polymarket_news_radar_history.jsonl"
DISPLAY_SEEN_PATH = STATE_DIR / "polymarket_news_radar_display_seen.json"
LATEST_PATH = STATE_DIR / "polymarket_news_radar_latest.json"
TRANSLATION_CACHE_PATH = STATE_DIR / "polymarket_news_radar_translations.json"
SOURCE_ALERT_STATE_PATH = STATE_DIR / "polymarket_news_radar_source_alert.json"
PROBE_SNAPSHOT_PATH = STATE_DIR / "polymarket_news_radar_probe_snapshot.json"
RUN_LOCK_PATH = STATE_DIR / "polymarket_news_radar.lock"

GAMMA_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = "fab-polymarket-news-radar/3.4"
RADAR_VERSION = "V3.4"

# Recalibrated 2026-07-25 on the population this penalty actually sees: 1007
# eligible signals out of 1405 raw candidates, replayed offline from one cached
# Gamma snapshot so every variant scores byte-identical market data.
# Real 24h volume is a long tail sitting on a floor of zeros:
#   p50 $0 · p60 $35 · p70 $1,012 · p75 $3,316 · p85 $17,075 · p90 $26,337
# V3.0's broken definition (which folded cumulative lifetime volume into the 24h
# number) at $25k left 35.9% of signals penalty-free. Under the corrected
# definition $1,000 leaves 30.2% — the closest practical match, so fixing the
# definition does not silently demote a third of the board. It is also p70, i.e.
# "there was any real trading at all today", and it is deliberately the same
# line as DIVERSITY_MIN_VOLUME_24H so that "thin market" means one thing here.
# Honest caveat: this knob does not move the output. Replaying the same snapshot
# at 0 / 2k / 5k / 10k / 25k / 100k / 2M gives identical eligible, displayed,
# strict and pushed counts and a byte-identical front page — the penalized tail
# is already excluded by the hard volume gate and the score floors. The value is
# chosen to state intent correctly, not because it changes what gets shown.
MIN_VOLUME_24H = 1_000.0
DISPLAY_SCORE = 45.0
MAX_CAPTION_SIGNALS = 6
DISPLAY_COOLDOWN_HOURS = 24 * 7
DISPLAY_SEEN_RETENTION_HOURS = 24 * 45
DEADLINE_GRACE_HOURS = 2.0
MARKET_LOOKAHEAD_DAYS = 14
MARKET_EXTENDED_LOOKAHEAD_DAYS = 90
# Gamma clamps every limit to 100 server-side (changelog 2026-05-14), so depth
# must come from pagination: 3 pages × 100 restores the coverage the pre-clamp
# limit=120 design intended, for ~3 extra requests (~2-4s) per run.
KEYSET_EVENT_PAGES = 3
KEYSET_MARKET_PAGES = 2
PUBLIC_SEARCH_LIMIT_PER_TYPE = 8
FETCH_RETRY_DELAY_SECONDS = 2.5
DISPLAY_FALLBACK_SCORE = 35.0
DIVERSITY_DISPLAY_SCORE = 18.0
DIVERSITY_MIN_VOLUME_24H = 1_000.0
DIGEST_MAIN_ITEMS = 3
DIGEST_CAPTION_MAX_CHARS = 900
DIGEST_MIN_VOLUME_24H = 25_000.0
# Front-page sports bar. This gate is applied to *displayed* items, not to the
# whole candidate set, and display is ranked by score rather than volume: on the
# 2026-07-25 snapshot the best displayed sports item was $77,486 of real 24h
# volume, so $1M and $300k both keep sports off the front page that day. $300k
# is still the better number because it is reachable in principle — sports does
# produce $1M+ markets (observed max $1,063,310) and those can surface on a big
# match day — whereas $1M against the corrected definition was set high enough
# to be a permanent lockout rather than a high bar. Not yet observed admitting
# sports on any real day; revisit if sports never fronts by the World Cup.
DIGEST_SPORTS_MIN_VOLUME_24H = 300_000.0
TARGET_DISPLAY_ITEMS = 18
BASE_SECTION_DISPLAY_ITEMS = 3
MAX_SECTION_DISPLAY_ITEMS = 4
SECTION_CANDIDATE_POOL_ITEMS = 60
# A section this thin has nothing to be picky with, so the score floor drops to
# the fallback tier rather than leaving the section empty.
THIN_SECTION_ITEMS = 3
SGT = timezone(timedelta(hours=8))

# LLM title translation (display-only; never feeds scoring or eligibility).
LLM_MODEL_DEFAULT = "gemini-2.5-flash"
# Measured 2026-07-25 against gemini-2.5-flash on the LAN: ~1.1s per title, so a
# single 24-title request overshot a 25s timeout. Chunking keeps each request
# comfortably inside the timeout and lets a late chunk fail without discarding
# the ones that already succeeded.
LLM_TIMEOUT = 45
LLM_MAX_TITLES = 24
LLM_BATCH_SIZE = 8
LLM_MAX_TITLE_CHARS = 220
TRANSLATION_CACHE_MAX_ENTRIES = 4_000
TRANSLATION_CACHE_TTL_DAYS = 60

# History rotation. The radar appends one record per run forever otherwise.
HISTORY_MAX_BYTES = 4 * 1024 * 1024
HISTORY_KEEP_RECORDS = 400


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    short_label: str
    emoji: str
    keywords: tuple[str, ...]
    weight: float = 1.0


CATEGORIES: tuple[Category, ...] = (
    Category(
        "macro_finance",
        "金融宏观",
        "Finance",
        "💵",
        (
            "fed",
            "fomc",
            "rate cut",
            "rate hike",
            "interest rate",
            "inflation",
            "cpi",
            "ppi",
            "jobs report",
            "unemployment",
            "recession",
            "gdp",
            "treasury",
            "yield",
            "tariff",
            "stock market",
            "s&p",
            "nasdaq",
            "dow",
            "oil",
            "gold",
            "silver",
            "dollar",
            "powell",
            "economy",
            "economic",
            "macro",
            "etf",
        ),
        1.16,
    ),
    Category(
        "crypto_web3",
        "Crypto / Web3",
        "Crypto",
        "₿",
        (
            "bitcoin",
            "btc",
            "ethereum",
            "eth",
            "solana",
            "sol",
            "crypto",
            "stablecoin",
            "tether",
            "usdt",
            "usdc",
            "coinbase",
            "binance",
            "xrp",
            "doge",
            "defi",
            "airdrop",
            "token",
            "memecoin",
        ),
        1.12,
    ),
    Category(
        "sports",
        "体育",
        "Sports",
        "🏟️",
        (
            "sports",
            "fifa",
            "world cup",
            "nba",
            "nfl",
            "mlb",
            "nhl",
            "soccer",
            "football",
            "tennis",
            "ufc",
            "formula 1",
            "f1",
            "wnba",
            "cricket",
            "esports",
            "champions league",
            "olympics",
            "premier league",
            "la liga",
            "match",
            "game",
            "win on",
        ),
        1.0,
    ),
    Category(
        "politics_policy",
        "政治 / 政策",
        "Politics",
        "🏛️",
        (
            "election",
            "president",
            "senate",
            "house",
            "congress",
            "supreme court",
            "policy",
            "bill",
            "law",
            "trump",
            "biden",
            "democrat",
            "republican",
            "poll",
            "approval",
            "cabinet",
            "governor",
            "mayor",
        ),
        1.1,
    ),
    Category(
        "geopolitics",
        "地缘 / 军事",
        "Geopolitics",
        "🌍",
        (
            "war",
            "ceasefire",
            "peace deal",
            "israel",
            "iran",
            "russia",
            "ukraine",
            "china",
            "taiwan",
            "nato",
            "sanction",
            "gaza",
            "hamas",
            "hezbollah",
            "north korea",
            "missile",
            "strike",
            "invasion",
            "military",
            "diplomacy",
            "geopolitics",
        ),
        1.18,
    ),
    Category(
        "tech_business",
        "科技 / 商业",
        "Tech",
        "🧠",
        (
            "openai",
            "spacex",
            "tesla",
            "nvidia",
            "apple",
            "google",
            "meta",
            "microsoft",
            "amazon",
            "xai",
            "anthropic",
            "ai",
            "ipo",
            "earnings",
            "merger",
            "acquisition",
            "antitrust",
            "startup",
            "product launch",
            "starship",
        ),
        1.08,
    ),
    Category(
        "entertainment_culture",
        "娱乐 / 文化",
        "Culture",
        "🎭",
        (
            "oscar",
            "oscars",
            "grammy",
            "emmy",
            "movie",
            "film",
            "box office",
            "album",
            "celebrity",
            "taylor swift",
            "pokemon",
            "netflix",
            "tv show",
            "youtube",
            "culture",
            "music",
            "game awards",
        ),
        0.92,
    ),
    Category(
        "society_weather_health_science",
        "社会 / 天气 / 健康 / 科学",
        "Society",
        "🌦️",
        (
            "weather",
            "hurricane",
            "wildfire",
            "earthquake",
            "temperature",
            "covid",
            "flu",
            "disease",
            "health",
            "vaccine",
            "climate",
            "nasa",
            "science",
            "medicine",
            "education",
            "school",
            "crime",
            "strike",
        ),
        0.96,
    ),
)

OTHER_CATEGORY = Category("other", "其他", "Other", "🧩", (), 0.7)

SECTION_DEFS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("geopolitics", "🌍 地缘军事", ("geopolitics",)),
    ("macro_finance", "💵 金融宏观", ("macro_finance",)),
    ("crypto_web3", "🪙 加密资产", ("crypto_web3",)),
    ("tech_business", "🧠 科技商业", ("tech_business",)),
    ("politics_policy", "🏛️ 政治政策", ("politics_policy",)),
    (
        "sports_culture",
        "🏟️ 体育文化",
        ("sports", "entertainment_culture"),
    ),
    # Last on purpose: the display allocator walks sections in order and stops
    # at TARGET_DISPLAY_ITEMS, so on a full board this tail section only keeps
    # what the six core sections left over — and when it has no candidates the
    # top-up loop hands its slots straight back to them.
    (
        "society_science",
        "🔬 社会科学",
        ("society_weather_health_science", "other"),
    ),
)

CATEGORY_TO_SECTION = {
    category_key: section_key
    for section_key, _section_label, category_keys in SECTION_DEFS
    for category_key in category_keys
}

CATEGORY_LABEL_CN = {
    "macro_finance": "金融宏观",
    "crypto_web3": "加密资产",
    "sports": "体育赛事",
    "politics_policy": "政治政策",
    "geopolitics": "地缘军事",
    "tech_business": "科技商业",
    "entertainment_culture": "娱乐文化",
    "society_weather_health_science": "社会科学",
    "other": "其他",
}

SECTION_COLOR_BLOCKS = {
    "geopolitics": "🟦",
    "macro_finance": "🟩",
    "crypto_web3": "🟨",
    "tech_business": "🟪",
    "politics_policy": "🟥",
    "sports_culture": "🟧",
    "society_science": "🟫",
}

SECTION_INTEREST_BONUS = {
    "geopolitics": 16.0,
    "macro_finance": 16.0,
    "crypto_web3": 12.0,
    "tech_business": 12.0,
    "politics_policy": 8.0,
    "sports_culture": -20.0,
    # Neutral: society/science stories get no standing boost, so only a
    # genuinely high raw score (major hurricane, pandemic declaration, a Nobel
    # with market heat) can reach the digest.
    "society_science": 0.0,
}

STORY_INTEREST_ADJUSTMENTS = {
    "us_iran_diplomatic_meeting": 8.0,
    "us_iran_agreement": 8.0,
    "iran_negotiation_terms": 6.0,
    "crude_oil_price_levels": 10.0,
    "hormuz_shipping": 8.0,
    "hormuz_security_incident": 8.0,
    "fed_rate_path": 12.0,
    "btc_price_levels": 10.0,
    "eth_price_levels": 6.0,
    "sol_price_levels": 4.0,
    "ai_product_timeline": 7.0,
    "elon_musk_tweet_count": -18.0,
    "game_market_props": -12.0,
    "match_exact_score": -12.0,
}

DIGEST_EXCLUDED_STORY_SUFFIXES = {
    "elon_musk_tweet_count",
    "game_market_props",
    "match_exact_score",
}

DIGEST_MAJOR_SPORTS_KEYWORDS = (
    "世界杯",
    "NBA",
    "NFL",
    "UFC",
    "温布尔登",
    "奥运",
    "奥斯卡",
    "欧洲杯",
    "金球奖",
)

VISUAL_SECTION_ACCENTS = {
    "geopolitics": "#2563eb",
    "macro_finance": "#047857",
    "crypto_web3": "#a16207",
    "tech_business": "#7e22ce",
    "politics_policy": "#b91c1c",
    "sports_culture": "#c2410c",
    "society_science": "#0f766e",
}

NUMBER_BADGES = ("1️⃣", "2️⃣", "3️⃣")

DISCOVERY_QUERIES_BY_SECTION: dict[str, tuple[str, ...]] = {
    "geopolitics": (
        "Iran",
        "Israel",
        "Ukraine",
        "Russia",
        "China Taiwan",
        "NATO",
        "ceasefire",
        "war",
    ),
    "macro_finance": (
        "Fed",
        "FOMC",
        "rate cut",
        "CPI",
        "oil",
        "gold",
        "recession",
        "tariff",
    ),
    "crypto_web3": (
        "Bitcoin",
        "Ethereum",
        "Solana",
        "crypto",
        "stablecoin",
        "ETF",
    ),
    "tech_business": (
        "AI",
        "OpenAI",
        "Anthropic",
        "Nvidia",
        "Tesla",
        "SpaceX",
        "Apple",
        "IPO",
    ),
    "politics_policy": (
        "election",
        "Trump",
        "Congress",
        "Supreme Court",
        "UK",
        "president",
        "poll",
    ),
    "sports_culture": (
        "World Cup",
        "NBA",
        "NFL",
        "UFC",
        "Wimbledon",
        "Oscars",
        "movie",
    ),
    "society_science": (
        "hurricane",
        "earthquake",
        "pandemic",
        "outbreak",
        "heat wave",
        "Nobel",
    ),
}

DISPLAY_TRANSLATION_STATUSES = {"ok", "needs_review"}


@dataclass
class Signal:
    event_id: str
    market_id: str
    category: Category
    title: str
    market_title: str
    outcome: str
    outcome_prices: tuple[tuple[str, float], ...]
    probability: float | None
    delta_1h: float | None
    delta_24h: float | None
    delta_1w: float | None
    volume_24h: float
    liquidity: float
    end_date: str | None
    effective_deadline_utc: str | None
    effective_deadline_sgt: str | None
    deadline_source: str | None
    ttl_hours: float | None
    eligibility_status: str
    reject_reason: str | None
    source_status: str
    category_reason: str
    translation_status: str
    score_breakdown: dict[str, float]
    event_url: str
    score: float
    reason: str
    # Filled in after scoring by the LLM translation pass. Empty means the rule
    # translator's output is what gets displayed.
    title_cn_override: str = ""
    translation_engine: str = "rule"

    @property
    def market_title_cn(self) -> str:
        return market_title_cn(self)


@dataclass
class CollectionResult:
    signals: list[Signal]
    rejected: list[dict[str, Any]]
    raw_candidate_count: int
    source_counts: dict[str, int]
    source_health: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportItem:
    story_key: str
    title: str
    signals: tuple[Signal, ...]
    score: float
    total_volume_24h: float

    @property
    def representative(self) -> Signal:
        return self.signals[0]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def sgt_label() -> str:
    return now_utc().astimezone(SGT).strftime("%Y-%m-%d %H:%M")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def sgt_datetime_label(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT")


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Fail closed: silently resetting corrupted state would re-send every
        # story the cooldown file was suppressing.
        raise RuntimeError(f"Invalid JSON state: {path}") from exc


def write_json(path: Path, payload: Any) -> None:
    # State files are read back by the next run; a crash mid-write must never
    # leave truncated JSON behind, so write to a sibling temp file and rename.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


_ACTIVE_LOCK_PATHS: set[Path] = set()


@contextmanager
def single_instance_lock(path: Path):
    """Refuse to run two radars against the same state directory at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path = path.resolve()
    if resolved_path in _ACTIVE_LOCK_PATHS:
        raise RuntimeError(f"Polymarket radar is already running: {path}")
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Polymarket radar is already running: {path}") from exc
        _ACTIVE_LOCK_PATHS.add(resolved_path)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started_at={iso_now()}\n")
        handle.flush()
        yield
    finally:
        _ACTIVE_LOCK_PATHS.discard(resolved_path)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def visible_html_len(value: str) -> int:
    return len(unescape(re.sub(r"<[^>]+>", "", value)))


def content_hash(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def fetch_json(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(2):
        if attempt:
            time.sleep(FETCH_RETRY_DELAY_SECONDS)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # 4xx is a request-contract problem a retry cannot fix.
            if error.code < 500 and error.code != 429:
                raise
            last_error = error
        except (OSError, json.JSONDecodeError) as error:
            last_error = error
    raise last_error  # type: ignore[misc]  # loop always sets it before falling through


FETCH_FAILURES: list[dict[str, str]] = []
# Wall-clock per fetch source, reset each run. A run spends its 75-110s mostly
# inside the five Gamma pulls; source_timings in latest.json is the only place
# that shows which source is the slow one when that budget drifts.
FETCH_TIMINGS: dict[str, dict[str, Any]] = {}


def reset_fetch_failures() -> None:
    FETCH_FAILURES.clear()
    FETCH_TIMINGS.clear()


def timed_fetch(source: str, fetch: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    started = time.monotonic()
    records = fetch()
    FETCH_TIMINGS[source] = {
        "seconds": round(time.monotonic() - started, 2),
        "records": len(records),
    }
    return records


def safe_fetch_json(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> Any | None:
    """Tolerate a single endpoint failing, but never lose the fact that it did.

    Every caller treats None as "this source is empty", which is
    indistinguishable from "Polymarket had nothing to say" unless the failure
    is recorded here for the run-level health check.
    """
    try:
        return fetch_json(url, params=params, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        FETCH_FAILURES.append(
            {
                "endpoint": url.replace(GAMMA_BASE, ""),
                "params": canonical_json(params or {})[:200],
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            }
        )
        return None


def unique_records(records: list[dict[str, Any]], identity_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for record in records:
        identity = next((str(record.get(field)) for field in identity_fields if record.get(field) not in (None, "")), "")
        if not identity:
            identity = json.dumps(record, sort_keys=True, ensure_ascii=False)[:160]
        if identity in seen:
            continue
        seen.add(identity)
        out.append(record)
    return out


def fetch_keyset_pages(
    endpoint: str,
    params: dict[str, Any],
    item_key: str,
    pages: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(max(1, pages)):
        page_params = {key: value for key, value in params.items() if value not in (None, "")}
        if cursor:
            page_params["after_cursor"] = cursor
        payload = safe_fetch_json(f"{GAMMA_BASE}{endpoint}", params=page_params)
        if not isinstance(payload, dict):
            break
        items = payload.get(item_key)
        if not isinstance(items, list) or not items:
            break
        out.extend(item for item in items if isinstance(item, dict))
        cursor_value = payload.get("next_cursor")
        cursor = str(cursor_value) if cursor_value else None
        if not cursor:
            break
    return out


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def field_value(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


MONTH_NUMBER_EN = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def infer_question_deadline(text: str, reference: datetime | None = None) -> tuple[datetime | None, str | None]:
    reference = reference or now_utc()
    patterns = (
        r"\bby\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(\d{4}))?",
        r"\bbefore\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(\d{4}))?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        month_name, day_text, year_text = match.groups()
        month = MONTH_NUMBER_EN[month_name.lower()]
        day = int(day_text)
        # Polymarket date markets typically resolve at the end of the named
        # UTC date. This inferred date is only used when it is earlier than
        # the API deadline, preventing stale "by yesterday" markets.
        candidates = [int(year_text)] if year_text else [reference.year, reference.year + 1]
        for year in candidates:
            try:
                resolved = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(days=1)
            except ValueError:
                # e.g. "by February 30" — a malformed question must not crash the run.
                continue
            # "by January 5" read in December means next January, not one that
            # already passed; without this the market looks expired and is
            # silently dropped.
            if year_text or resolved > reference:
                return resolved, "question.by_date"
        return None, None
    return None, None


def effective_deadline(event: dict[str, Any], market: dict[str, Any]) -> tuple[datetime | None, str | None]:
    inferred, inferred_source = infer_question_deadline(str(market.get("question") or event.get("title") or ""))
    fields: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
        ("market.endDateIso", market, ("endDateIso",)),
        ("market.endDate", market, ("endDate",)),
        ("market.umaEndDateIso", market, ("umaEndDateIso",)),
        ("market.umaEndDate", market, ("umaEndDate",)),
        ("market.gameStartTime", market, ("gameStartTime",)),
        ("event.endDateIso", event, ("endDateIso",)),
        ("event.endDate", event, ("endDate",)),
    )
    for label, payload, names in fields:
        dt = parse_datetime(field_value(payload, *names))
        if dt is not None:
            if inferred is not None and inferred < dt:
                return inferred, inferred_source
            return dt, label
    return inferred, inferred_source


def ttl_hours(deadline: datetime | None, now: datetime | None = None) -> float | None:
    if deadline is None:
        return None
    now = now or now_utc()
    return (deadline - now).total_seconds() / 3600


def market_keys(market: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for key in ("id", "conditionId", "slug", "clobTokenIds"):
        value = market.get(key)
        if value in (None, ""):
            continue
        keys.add(str(value))
    question = market.get("question")
    if question:
        keys.add(f"q:{question}")
    return keys


def is_false(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if value is None:
        return False
    return str(value).strip().lower() in {"false", "0", "no"}


def is_true(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"true", "1", "yes"}


def clean_text(value: str, limit: int = 120) -> str:
    value = " ".join(value.strip().split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def format_prob_cn(value: float | None) -> str:
    if value is None:
        return "暂无"
    return f"{value * 100:.1f}%"


def format_delta_cn(value: float | None) -> str:
    if value is None:
        return "暂无"
    return f"{value * 100:+.1f}点"


def format_money_cn(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿美元"
    if abs_value >= 10_000:
        return f"{value / 10_000:.0f}万美元"
    if abs_value >= 1_000:
        return f"{value / 1_000:.0f}千美元"
    return f"{value:.0f}美元"


def strongest_signal(signals: list[Signal]) -> Signal:
    return max(signals, key=lambda signal: signal.score)


MONTHS_CN = {
    "january": "1月",
    "february": "2月",
    "march": "3月",
    "april": "4月",
    "may": "5月",
    "june": "6月",
    "july": "7月",
    "august": "8月",
    "september": "9月",
    "october": "10月",
    "november": "11月",
    "december": "12月",
}


TERM_REPLACEMENTS_CN: tuple[tuple[str, str], ...] = (
    ("United States", "美国"),
    ("US x Iran", "美国与伊朗"),
    ("U.S.", "美国"),
    ("US", "美国"),
    ("Iran", "伊朗"),
    ("Israel", "以色列"),
    ("Russia", "俄罗斯"),
    ("Ukraine", "乌克兰"),
    ("China", "中国"),
    ("Taiwan", "台湾"),
    ("Bitcoin", "比特币"),
    ("BTC", "比特币"),
    ("Ethereum", "以太坊"),
    ("ETH", "以太坊"),
    ("Solana", "Solana"),
    ("Google", "谷歌"),
    ("Alphabet", "谷歌母公司"),
    ("Nvidia", "英伟达"),
    ("Apple", "苹果"),
    ("Microsoft", "微软"),
    ("Tesla", "特斯拉"),
    ("Amazon", "亚马逊"),
    ("Meta", "Meta"),
    ("SpaceX", "SpaceX"),
    ("Elon Musk", "马斯克"),
    ("Trump", "特朗普"),
    ("FOMC", "美联储议息会议"),
    ("Fed", "美联储"),
    ("permanent peace deal", "永久和平协议"),
    ("ceasefire extension", "停火延期"),
    ("new agreement", "新协议"),
    ("agreement", "协议"),
    ("tweets", "发帖"),
    ("tweet", "发帖"),
    ("sign", "签署"),
    ("rate cut", "降息"),
    ("rate hike", "加息"),
    ("25 bps", "25个基点"),
    ("bps", "个基点"),
    ("market cap", "市值"),
    ("above", "高于"),
    ("below", "低于"),
    ("reach", "触及"),
    ("hit", "触及"),
    ("win", "赢得"),
    # No bare function words (by/on/from/to/in/and/post): \b treats a hyphen as
    # a boundary, so "by-election" became "截至-election" and "Ninjas in
    # Pyjamas" became "Ninjas在Pyjamas" in production. Titles they would have
    # half-translated stay English-heavy either way and are already routed to
    # the LLM pass via needs_review grading.
)


def translate_months(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        month = MONTHS_CN[match.group(1).lower()]
        day = int(match.group(2))
        year = match.group(3)
        return f"{year}年{month}{day}日" if year else f"{month}{day}日"

    return re.sub(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,?\s*(\d{4}))?",
        repl,
        text,
        flags=re.IGNORECASE,
    )


def localize_market_text(text: str, limit: int = 92) -> str:
    localized = translate_months(clean_text(text, limit)).strip("? ")
    # "至" only when the dash sits between date-ish sides ("7月18日 - 25");
    # a title separator like "(BO3) - The International" must keep its dash.
    localized = re.sub(
        r"(?<=[0-9日])\s*[–—-]\s*(?=\d|January|February|March|April|May|June|July"
        r"|August|September|October|November|December)",
        "至",
        localized,
        flags=re.IGNORECASE,
    )
    localized = re.sub(r"\s+", " ", localized).strip()
    for src, dest in TERM_REPLACEMENTS_CN:
        # "US" stays case-sensitive: IGNORECASE would also hit the pronoun "us".
        flags = 0 if src == "US" else re.IGNORECASE
        localized = re.sub(rf"\b{re.escape(src)}\b", dest, localized, flags=flags)
    localized = localized.replace("# 发帖", "发帖数量").replace("#发帖", "发帖数量")
    localized = re.sub(r"^Will the price of (.+?) be 高于 (.+)$", r"\1价格是否高于 \2", localized, flags=re.IGNORECASE)
    localized = re.sub(r"^Will (.+?) be 高于 (.+)$", r"\1是否高于 \2", localized, flags=re.IGNORECASE)
    localized = re.sub(r"^Will (.+?) 触及 (.+)$", r"\1是否触及 \2", localized, flags=re.IGNORECASE)
    localized = re.sub(r"^Will (.+)$", r"是否\1", localized, flags=re.IGNORECASE)
    localized = localized.replace("$", "")
    return localized.strip()


def asset_name_cn(value: str) -> str:
    names = {
        "bitcoin": "比特币",
        "btc": "比特币",
        "ethereum": "以太坊",
        "eth": "以太坊",
        "solana": "Solana",
        "sol": "Solana",
    }
    return names.get(value.lower(), value)


def company_name_cn(value: str) -> str:
    names = {
        "google": "谷歌",
        "alphabet": "谷歌母公司",
        "nvidia": "英伟达",
        "apple": "苹果",
        "microsoft": "微软",
        "tesla": "特斯拉",
        "amazon": "亚马逊",
        "meta": "Meta",
        "spacex": "太空探索技术公司",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "xai": "xAI",
        "meituan": "美团",
        "bytedance": "字节跳动",
        "moonshot": "月之暗面",
        "alibaba": "阿里巴巴",
    }
    return names.get(value.lower(), value)


def person_name_cn(value: str) -> str:
    names = {
        "hakeem jeffries": "哈基姆·杰弗里斯",
        "fujimori": "藤森",
        "andy burnham": "安迪·伯纳姆",
        "trump": "特朗普",
        "starmer": "斯塔默",
        "keir starmer": "基尔·斯塔默",
        "lebron james": "勒布朗·詹姆斯",
        "phil weiser": "菲尔·韦泽",
        "gustavo petro": "古斯塔沃·佩特罗",
        "diana degette": "戴安娜·德格特",
        "gavin newsom": "加文·纽森",
        "wes streeting": "韦斯·斯特里廷",
        "samuel alito": "塞缪尔·阿利托",
        "edi rama": "埃迪·拉马",
        "mitch mcconnell": "米奇·麦康奈尔",
        "kylian mbappe": "基利安·姆巴佩",
        "benjamin netanyahu": "本雅明·内塔尼亚胡",
    }
    return names.get(value.lower(), value)


def sports_name_cn(value: str) -> str:
    names = {
        "united states": "美国",
        "usa": "美国",
        "us": "美国",
        "spain": "西班牙",
        "england": "英格兰",
        "france": "法国",
        "senegal": "塞内加尔",
        "south korea": "韩国",
        "new zealand": "新西兰",
        "egypt": "埃及",
        "ir iran": "伊朗",
        "iran": "伊朗",
        "iraq": "伊拉克",
        "norway": "挪威",
        "switzerland": "瑞士",
        "makerfield": "梅克菲尔德",
        "portugal": "葡萄牙",
        "united kingdom": "英国",
        "the united kingdom": "英国",
        "dr congo": "刚果民主共和国",
        "new york knicks": "纽约尼克斯",
        "argentina": "阿根廷",
        "algeria": "阿尔及利亚",
        "brazil": "巴西",
        "germany": "德国",
        "ecuador": "厄瓜多尔",
        "colombia": "哥伦比亚",
        "panama": "巴拿马",
        "jordan": "约旦",
        "croatia": "克罗地亚",
        "cape verde": "佛得角",
        "scotland": "苏格兰",
        "belgium": "比利时",
        "cincinnati bengals": "辛辛那提猛虎",
        "miami heat": "迈阿密热火",
        "arizona diamondbacks": "亚利桑那响尾蛇",
        "st. louis cardinals": "圣路易斯红雀",
        "atlanta braves": "亚特兰大勇士",
        "san francisco giants": "旧金山巨人",
        "atlanta dream": "亚特兰大梦想",
        "golden state valkyries": "金州女武神",
    }
    return names.get(value.lower(), value)


def usd_amount_cn(value: str) -> str:
    text = value.strip().replace("$", "")
    upper = text.upper()
    if upper.endswith("T"):
        return f"{text[:-1]}万亿美元"
    if upper.endswith("B"):
        return f"{text[:-1]}亿美元"
    if upper.endswith("M"):
        return f"{text[:-1]}百万美元"
    return f"{text}美元"


def date_phrase_cn(value: str) -> str:
    raw = value.strip("? ")
    raw = re.sub(r"^(?:in|on|by|before|after)\s+", "", raw, flags=re.IGNORECASE)
    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{year}年{int(month)}月{int(day)}日"
    if re.fullmatch(r"\d{4}", raw):
        return f"{raw}年"
    month_match = re.fullmatch(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)(?:,?\s*(\d{4}))?",
        raw,
        flags=re.IGNORECASE,
    )
    if month_match:
        month = MONTHS_CN[month_match.group(1).lower()]
        year = month_match.group(2)
        return f"{year}年{month}" if year else month
    end_match = re.fullmatch(
        r"end of (January|February|March|April|May|June|July|August|September|October|November|December)(?:,\s*(\d{4}))?",
        raw,
        flags=re.IGNORECASE,
    )
    if end_match:
        month = MONTHS_CN[end_match.group(1).lower()]
        year = end_match.group(2)
        return f"{year}年{month}底" if year else f"{month}底"
    text = translate_months(raw)
    text = text.replace(" - ", "至").replace(" – ", "至").replace(" — ", "至").replace(" to ", "至")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def event_context_cn(signal: Signal, limit: int = 34) -> str:
    title = signal.title or signal.market_title
    text = localize_market_text(title, limit=100)
    text = text.replace("Counter-Strike", "反恐精英").replace("League of Legends", "英雄联盟")
    text = text.replace("LoL", "英雄联盟")
    text = re.sub(r"\s+vs\s+", " 对 ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return clean_text(text, limit)


def translate_outcome(value: str, limit: int = 36) -> str:
    text = translate_months(clean_text(value, limit)).strip("? ")
    lower = value.lower()
    if text.startswith("↑"):
        return "高于 " + text[1:].strip()
    if text.startswith("↓"):
        return "低于 " + text[1:].strip()
    if "25 bps" in lower:
        return "一次25个基点"
    if "google" in lower:
        return "谷歌"
    if "civilian service act" in lower:
        return "公民服务法案"
    if "eugen tomac" in lower:
        return "该候选人"
    if "fight won by submission" in lower or "submission" in lower:
        return "降服获胜"
    if "game handicap" in lower:
        return "比赛让分"
    if "2nd half" in lower and "o/u" in lower:
        number = re.search(r"o/u\s*([0-9.]+)", lower)
        return f"下半场总分高于/低于{number.group(1) if number else ''}".strip()
    map_match = re.search(r"map\s*(\d+)", lower)
    map_label = f"第{map_match.group(1)}图" if map_match else "地图"
    if "total rounds" in lower and "over/under" in lower:
        number = re.search(r"over/under\s*([0-9.]+)", lower)
        return f"{map_label}总回合数高于/低于{number.group(1) if number else ''}".strip()
    if "rounds handicap" in lower:
        return f"{map_label}回合让分"
    if "winner" in lower:
        return f"{map_label}胜者"
    if "any player penta kill" in lower:
        return "是否出现五杀"
    if lower in {"yes", "no"}:
        return "是" if lower == "yes" else "否"
    if re.search(r"[A-Za-z]", text):
        return "该结果"
    return text


def translate_option_label(value: str, limit: int = 28) -> str:
    lower = value.lower().strip()
    if lower in {"yes", "y"}:
        return "是"
    if lower in {"no", "n"}:
        return "否"
    if lower == "up":
        return "上涨"
    if lower == "down":
        return "下跌"
    if lower == "over":
        return "高于"
    if lower == "under":
        return "低于"
    sports_label = sports_name_cn(value.strip())
    if sports_label != value.strip():
        return sports_label
    text = translate_outcome(value, limit=limit)
    if text == "该结果":
        text = localize_market_text(value, limit=limit)
    return clean_text(text, limit)


def format_options_cn(signal: Signal, max_options: int = 4) -> str:
    pairs = list(signal.outcome_prices)
    if not pairs and signal.probability is not None:
        pairs = [(signal.outcome, signal.probability)]
        if 0.0 <= signal.probability <= 1.0:
            pairs.append(("No", 1.0 - signal.probability))
    if not pairs:
        return "选项：暂无可用概率"

    binary = {label.lower().strip() for label, _prob in pairs}
    if binary == {"yes", "no"}:
        yes_prob = next((prob for label, prob in pairs if label.lower().strip() == "yes"), None)
        no_prob = next((prob for label, prob in pairs if label.lower().strip() == "no"), None)
        return (
            f"选项：是 <code>{format_prob_cn(yes_prob)}</code>｜"
            f"否 <code>{format_prob_cn(no_prob)}</code>"
        )

    visible = pairs[:max_options]
    parts = [
        f"{escape(translate_option_label(label))} <code>{format_prob_cn(prob)}</code>"
        for label, prob in visible
    ]
    if len(pairs) > max_options:
        parts.append(f"另{len(pairs) - max_options}项未列")
    return "选项：" + "｜".join(parts)


# English tokens a Chinese title may legitimately keep. Shared by the quality
# check below and by the LLM prompt, so the two cannot drift into disagreeing
# about what "translated" means.
TRANSLATION_ALLOWED_IDENTIFIERS = frozenset(
    {
        "ai",
        "btc",
        "cpi",
        "eth",
        "etf",
        "fdv",
        "fifa",
        "fomc",
        "gdp",
        "gemini",
        "gpt",
        "iphone",
        "ipo",
        "meta",
        "metamask",
        "nfl",
        "openai",
        "pro",
        "solana",
        "spacex",
        "spy",
        "wti",
        "xagusd",
        "xauusd",
        "xrp",
    }
)


def title_translation_status(title: str) -> str:
    if re.search(r"是否(?:the|no|there|lebron|starmer)\b", title, flags=re.IGNORECASE):
        return "needs_review"
    if re.search(r"\b(Will|Game \d|Games Total|O/U|Both Teams Slay|Exact Score)\b", title, flags=re.IGNORECASE):
        return "needs_review"
    if re.search(
        r"\b(the|there|rate cuts|interest rates|play for|out by| dip | in June| be | for | customers| agree | unfreeze| enrichment| Group )\b",
        title,
        flags=re.IGNORECASE,
    ):
        return "needs_review"
    english_tokens = {token.lower() for token in re.findall(r"[A-Za-z]{2,}", title)}
    if english_tokens - TRANSLATION_ALLOWED_IDENTIFIERS:
        return "needs_review"
    return "ok"


def market_title_cn(signal: Signal) -> str:
    if signal.title_cn_override:
        return signal.title_cn_override
    raw = signal.market_title or signal.title
    lower = raw.lower()
    outcome = translate_outcome(signal.outcome)
    localized = localize_market_text(raw)

    match = re.search(
        r"Will the price of (Bitcoin|Ethereum|Solana) be above \$?([0-9,.]+)\s+on\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        asset, price, date_text = match.groups()
        return f"{asset_name_cn(asset)}在{date_phrase_cn(date_text)}是否高于{usd_amount_cn(price)}"

    match = re.search(
        r"Will (Bitcoin|Ethereum|Solana) reach \$?([0-9,.]+)\s+(?:on\s+)?(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        asset, price, date_text = match.groups()
        return f"{asset_name_cn(asset)}在{date_phrase_cn(date_text)}是否触及{usd_amount_cn(price)}"

    match = re.search(
        r"Will (Bitcoin|Ethereum|Solana) dip to \$?([0-9,.]+)\s+in\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        asset, price, date_text = match.groups()
        return f"{asset_name_cn(asset)}在{date_phrase_cn(date_text)}是否跌至{usd_amount_cn(price)}"

    match = re.search(
        r"Will (Google|Alphabet|Nvidia|Apple|Microsoft|Tesla|Amazon|Meta) be above \$?([0-9.]+[TBM]?)\s+on\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        company, value, date_text = match.groups()
        return f"{company_name_cn(company)}在{date_phrase_cn(date_text)}市值是否高于{usd_amount_cn(value)}"

    match = re.search(
        r"Will SpaceX'?s valuation hit \((HIGH|LOW)\) \$?([0-9.]+[TBM]?)\s+by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        direction, value, date_text = match.groups()
        direction_cn = "高点" if direction.upper() == "HIGH" else "低点"
        return f"{company_name_cn('spacex')}估值是否在{date_phrase_cn(date_text)}前触及{usd_amount_cn(value)}（{direction_cn}）"

    match = re.search(
        r"Will OpenAI'?s valuation hit \((HIGH|LOW)\) \$?([0-9.]+[TBM]?)\s+by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        direction, value, date_text = match.groups()
        direction_cn = "高点" if direction.upper() == "HIGH" else "低点"
        return f"OpenAI 估值是否在{date_phrase_cn(date_text)}前触及{usd_amount_cn(value)}（{direction_cn}）"

    match = re.search(r"US x Iran permanent peace deal by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"美国与伊朗是否在{date_phrase_cn(match.group(1))}前达成永久和平协议"

    match = re.search(r"US and Iran sign an agreement by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"美国与伊朗是否在{date_phrase_cn(match.group(1))}前签署协议"

    match = re.search(
        r"US announces new Iran agreement/ceasefire extension by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"美国是否在{date_phrase_cn(match.group(1))}前宣布新的伊朗协议或停火延期"

    match = re.search(r"Israel x Iran permanent peace deal by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"以色列与伊朗是否在{date_phrase_cn(match.group(1))}前达成永久和平协议"

    match = re.search(r"US x Iran diplomatic meeting by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"美国与伊朗是否在{date_phrase_cn(match.group(1))}前举行外交会谈"

    match = re.search(r"Will the next diplomatic US-Iran meeting be in (.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"下一次美国-伊朗外交会谈是否在{sports_name_cn(match.group(1))}举行"

    match = re.search(
        r"Will Trump agree to Iranian oil sanction relief by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"特朗普是否在{date_phrase_cn(match.group(1))}前同意放松伊朗石油制裁"

    match = re.search(
        r"Strait of Hormuz traffic returns to normal by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"霍尔木兹海峡航运是否在{date_phrase_cn(match.group(1))}前恢复正常"

    match = re.search(
        r"Iran successfully targets shipping (?:by|on)\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"伊朗是否在{date_phrase_cn(match.group(1))}前成功袭击航运目标"

    match = re.search(
        r"Will (.+?) send (.+?) (?:to|through) (?:the\s+)?Strait of Hormuz by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        country, target, date_text = match.groups()
        country = re.sub(r"^the\s+", "", country, flags=re.IGNORECASE)
        target_cn = "军舰" if "ship" in target.lower() else localize_market_text(target, limit=24)
        return f"{sports_name_cn(country)}是否在{date_phrase_cn(date_text)}前向霍尔木兹海峡派出{target_cn}"

    match = re.search(
        r"Will there be between (.+?) and (.+?) average daily (.+?) of (?:the\s+)?Strait of Hormuz (?:by|on)\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        low, high, target, date_text = match.groups()
        target_cn = (
            "船只通行量"
            if any(word in target.lower() for word in ("ship", "vessel", "transit"))
            else localize_market_text(target, limit=24)
        )
        return f"霍尔木兹海峡在{date_phrase_cn(date_text)}前日均{target_cn}是否为{low}-{high}"

    match = re.search(
        r"Will ([0-9,.]+) (?:ships|vessels) transit (?:the\s+)?Strait of Hormuz on any day by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        count, date_text = match.groups()
        return f"霍尔木兹海峡是否在{date_phrase_cn(date_text)}前单日通行量达到{count}艘船"

    match = re.search(
        r"Will Israeli forces withdraw from beyond the Litani River by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"以色列军队是否在{date_phrase_cn(match.group(1))}前撤出利塔尼河以北地区"

    match = re.search(
        r"Will the text of the US-Iran agreement be released by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"美国-伊朗协议文本是否在{date_phrase_cn(match.group(1))}前发布"

    match = re.search(r"Will no Fed rate cuts happen in\s+(\d{4})\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}年是否没有美联储降息"

    match = re.search(
        r"Will there be no change in Fed interest rates after the (.+?) meeting\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{date_phrase_cn(match.group(1))}会议后美联储利率是否不变"

    match = re.search(r"Starmer out by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{person_name_cn('starmer')}是否在{date_phrase_cn(match.group(1))}前下台"

    match = re.search(r"Will Keir Starmer be the next leader out before\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{person_name_cn('keir starmer')}是否在{date_phrase_cn(match.group(1))}前成为下一位下台领导人"

    match = re.search(r"Will (.+?) be the next leader out before\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        person, date_text = match.groups()
        return f"{person_name_cn(person)}是否在{date_phrase_cn(date_text)}前成为下一位下台领导人"

    match = re.search(
        r"Will LeBron James play for the New York Knicks in\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{person_name_cn('lebron james')}是否在{match.group(1)}赛季效力{sports_name_cn('new york knicks')}"

    match = re.search(
        r"Will Claude Fable 5 be restored for US customers by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"Claude Fable 5 是否在{date_phrase_cn(match.group(1))}前恢复对美国用户服务"

    match = re.search(
        r"Will GPT-([0-9.]+) be released between (.+?) and (.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        version, start_date, end_date = match.groups()
        return f"GPT-{version} 是否在{date_phrase_cn(start_date)}至{date_phrase_cn(end_date)}期间发布"

    match = re.search(r"GPT-([0-9.]+) released by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        version, date_text = match.groups()
        return f"GPT-{version} 是否在{date_phrase_cn(date_text)}前发布"

    match = re.search(r"Will GPT-([0-9.]+) not be released by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        version, date_text = match.groups()
        return f"GPT-{version} 是否在{date_phrase_cn(date_text)}前不发布"

    match = re.search(r"Will the next (.+?) model be released (?:by|on|in)\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        model, date_text = match.groups()
        model_cn = localize_market_text(model, limit=32)
        return f"下一代{model_cn}模型是否在{date_phrase_cn(date_text)}发布"

    match = re.search(
        r"Will Trump agree to unfreeze Iranian assets by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"特朗普是否在{date_phrase_cn(match.group(1))}前同意解冻伊朗资产"

    match = re.search(
        r"Will Trump agree to Iranian enrichment of uranium by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"特朗普是否在{date_phrase_cn(match.group(1))}前同意伊朗铀浓缩"

    match = re.search(
        r"Will (France|Argentina|Algeria) win Group ([A-Z]) in the 2026 FIFA World Cup\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        team, group = match.groups()
        return f"{sports_name_cn(team)}是否赢得2026年世界杯{group}组"

    match = re.search(r"Will (.+?) win on (\d{4}-\d{2}-\d{2})\??$", raw, flags=re.IGNORECASE)
    if match:
        team, date_text = match.groups()
        return f"{sports_name_cn(team)}是否在{date_phrase_cn(date_text)}获胜"

    match = re.fullmatch(r"(.+?) vs\. (.+?)", raw.strip(), flags=re.IGNORECASE)
    if match:
        team_a, team_b = match.groups()
        return f"{sports_name_cn(team_a)} 对 {sports_name_cn(team_b)}"

    match = re.search(r"Will (.+?) win the 2026 FIFA World Cup\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{sports_name_cn(match.group(1))}是否赢得2026年世界杯"

    match = re.search(
        r"Will Kylian Mbappe be the top goalscorer at the 2026 FIFA World Cup\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{person_name_cn('kylian mbappe')}是否成为2026年世界杯最佳射手"

    match = re.search(
        r"Will (.+?) advance to the knockout stages at the 2026 FIFA World Cup\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{sports_name_cn(match.group(1))}是否晋级2026年世界杯淘汰赛"

    match = re.search(r"Will (.+?) win the (\d{4}) NFL league championship\??$", raw, flags=re.IGNORECASE)
    if match:
        team, year = match.groups()
        team = re.sub(r"^the\s+", "", team, flags=re.IGNORECASE)
        return f"{sports_name_cn(team)}是否赢得{year}年NFL总冠军"

    match = re.search(r"Will (.+?) be the (\d{4}) Men.?s Wimbledon winner\??$", raw, flags=re.IGNORECASE)
    if match:
        player, year = match.groups()
        return f"{player}是否赢得{year}年温网男单冠军"

    match = re.search(r"Will the highest temperature in (.+?) be ([0-9.]+°?C) on (.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        city, temperature, date_text = match.groups()
        return f"{city}在{date_phrase_cn(date_text)}最高气温是否为{temperature}"

    match = re.search(r"US-Iran Final Nuclear Deal by (.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"美国与伊朗是否在{date_phrase_cn(match.group(1))}前达成最终核协议"

    match = re.search(r"Iran full airspace closure by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"伊朗是否在{date_phrase_cn(match.group(1))}前全面关闭领空"

    match = re.search(r"Will Iran announce withdrawal from MOU negotiations by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"伊朗是否在{date_phrase_cn(match.group(1))}前宣布退出谅解备忘录谈判"

    match = re.search(r"(?:Will\s+)?Iran charges? Hormuz fees by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"伊朗是否在{date_phrase_cn(match.group(1))}前向霍尔木兹海峡通行收费"

    match = re.search(r"Will (.+?) enter Iran by (.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        person, date_text = match.groups()
        return f"{person_name_cn(person)}是否在{date_phrase_cn(date_text)}前进入伊朗"

    match = re.search(r"Will Ukraine recapture Crimean territory by (.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"乌克兰是否在{date_phrase_cn(match.group(1))}前收复克里米亚领土"

    match = re.search(r"US obtains Iranian enriched uranium by (.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"美国是否在{date_phrase_cn(match.group(1))}前取得伊朗浓缩铀"

    match = re.search(r"Will Russia capture all of (.+?) by (.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        place, date_text = match.groups()
        return f"俄罗斯是否在{date_phrase_cn(date_text)}前占领{place}全境"

    match = re.search(r"Will (.+?) win the next Russian parliamentary election\??$", raw, flags=re.IGNORECASE)
    if match:
        party = match.group(1).replace("(ER)", "").replace("(NL)", "").strip()
        party_names = {"new people": "新人民党", "united russia": "统一俄罗斯党"}
        return f"{party_names.get(party.lower(), party)}是否在下一次俄罗斯议会选举中获得最多席位"

    match = re.search(
        r"Will (.+?) gain the most seats in the next Russian parliamentary election\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        party = match.group(1).replace("(ER)", "").replace("(NL)", "").strip()
        party_names = {"new people": "新人民党", "united russia": "统一俄罗斯党"}
        return f"{party_names.get(party.lower(), party)}是否在下一次俄罗斯议会选举中获得最多席位"

    match = re.search(r"Will Benjamin Netanyahu be the next Prime Minister of Israel\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{person_name_cn('benjamin netanyahu')}是否成为以色列下一任总理"

    match = re.search(r"Will Wes Streeting be the next Foreign Secretary of the (?:UK|United Kingdom)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{person_name_cn('wes streeting')}是否成为英国下一任外交大臣"

    match = re.search(r"Will Samuel Alito announce his retirement by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{person_name_cn('samuel alito')}是否在{date_phrase_cn(match.group(1))}前宣布退休"

    match = re.search(r"Edi Rama out as Albania PM in\s+(\d{4})\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"{person_name_cn('edi rama')}是否在{match.group(1)}年卸任阿尔巴尼亚总理"

    match = re.search(
        r"Will Mitch McConnell step down from the Senate before his term ends\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{person_name_cn('mitch mcconnell')}是否在任期结束前辞去参议员职务"

    match = re.search(r"Will (.+?) win the (\d{4}) (.+?) election\??$", raw, flags=re.IGNORECASE)
    if match:
        person, year, race = match.groups()
        race_key = race.lower().strip()
        race_names = {
            "colorado governor democratic primary": "科罗拉多州州长民主党初选",
            "peruvian presidential": "秘鲁总统",
            "new york 8th district": "纽约第8选区",
        }
        race_cn = race_names.get(race_key, localize_market_text(race, limit=48))
        suffix = "" if race_cn.endswith(("初选", "选举")) else "选举"
        return f"{person_name_cn(person)}是否赢得{year}年{race_cn}{suffix}"

    match = re.search(r"US recession by end of\s+(\d{4})\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"美国是否在{match.group(1)}年底前出现经济衰退"

    match = re.search(
        r"Will China GDP growth in Q([1-4])\s+(\d{4}) be between ([0-9.]+)% and ([0-9.]+)%\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        quarter, year, low, high = match.groups()
        return f"中国{year}年第{quarter}季度GDP增速是否为{low}%-{high}%"

    match = re.search(
        r"(Gold|Silver)\s+\((?:XAUUSD|XAGUSD)\)\s+hit\s+\((HIGH|LOW)\)\s+\$?([0-9,.]+)\s+(?:by|in|week of)\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        metal, direction, price, date_text = match.groups()
        direction_cn = "高点" if direction.upper() == "HIGH" else "低点"
        metal_cn = "黄金" if metal.lower() == "gold" else "白银"
        return f"{metal_cn}在{date_phrase_cn(date_text)}是否触及{usd_amount_cn(price)}（{direction_cn}）"

    match = re.search(
        r"Gavin Newsom or his wife federally charged by\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{person_name_cn('gavin newsom')}或其妻子是否在{date_phrase_cn(match.group(1))}前受到联邦指控"

    match = re.search(
        r"Will (?:WTI\s+)?Crude Oil(?:\s+\([A-Z]+\))? hit \((HIGH|LOW)\) \$?([0-9,.]+)\s+(?:by|in)\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        direction, price, date_text = match.groups()
        direction_cn = "高点" if direction.upper() == "HIGH" else "低点"
        return f"原油在{date_phrase_cn(date_text)}是否触及{usd_amount_cn(price)}（{direction_cn}）"

    match = re.search(r"(Bitcoin|Ethereum|Solana) Up or Down on\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        asset, date_text = match.groups()
        return f"{asset_name_cn(asset)}在{date_phrase_cn(date_text)}涨跌方向"

    match = re.search(
        r"Will Elon Musk post ([0-9]+-[0-9]+) tweets from (.+?) to (.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        tweet_range, start_date, end_date = match.groups()
        return f"马斯克在{date_phrase_cn(start_date)}至{date_phrase_cn(end_date)}期间发帖数量是否为 {tweet_range}"

    match = re.search(r"Elon Musk # tweets\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match and outcome != "该结果":
        return f"马斯克在{date_phrase_cn(match.group(1))}期间发帖数量是否为 {outcome}"

    match = re.search(
        r"Will Tesla deliver ([0-9,]+) or more vehicles in Q([1-4])\s+(\d{4})\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        count, quarter, year = match.groups()
        return f"特斯拉在{year}年第{quarter}季度交付量是否达到{count}辆或以上"

    match = re.search(
        r"Will Tesla deliver between ([0-9,]+) and ([0-9,]+) vehicles in Q([1-4])\s+(\d{4})\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        low, high, quarter, year = match.groups()
        return f"特斯拉在{year}年第{quarter}季度交付量是否为{low}-{high}辆"

    match = re.search(
        r"Will (NVIDIA|Amazon|Microsoft|Alphabet|Apple|Tesla) be the largest company in the world by market cap on\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        company, date_text = match.groups()
        return f"{company_name_cn(company)}是否在{date_phrase_cn(date_text)}成为全球市值最高公司"

    match = re.search(
        r"Will (Google|xAI|Meituan|Moonshot|ByteDance|Alibaba|Meta) have the best AI model at the end of\s+(.+?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        company, date_text = match.groups()
        return f"{company_name_cn(company)}是否在{date_phrase_cn(date_text)}拥有最强AI模型"

    match = re.search(r"Will Apple release a foldable iPhone before\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"苹果是否在{date_phrase_cn(match.group(1))}前发布折叠屏 iPhone"

    match = re.search(r"Will SpaceX have the highest IPO market cap(?: in)?\s+(\d{4})\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"太空探索技术公司是否成为{match.group(1)}年IPO市值最高公司"

    match = re.search(r"(?:Will\s+)?MetaMask launch a token by\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"MetaMask是否在{date_phrase_cn(match.group(1))}前发行代币"

    match = re.search(
        r"(.+?) vs\. (.+?): (.+?) 2nd Half O/U ([0-9.]+)",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        team_a, team_b, target_team, line = match.groups()
        return (
            f"{sports_name_cn(team_a)} 对 {sports_name_cn(team_b)}："
            f"{sports_name_cn(target_team)}下半场得分高于/低于 {line}"
        )

    match = re.search(r"Game\s+(\d+): Both Teams Slay a Dragon\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"第{match.group(1)}局双方是否都击杀小龙"

    match = re.search(r"Games Total: O/U ([0-9.]+)\??$", raw, flags=re.IGNORECASE)
    if match:
        return f"总局数高于/低于 {match.group(1)}"

    match = re.search(r"Exact Score:\s+(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+?)\??$", raw, flags=re.IGNORECASE)
    if match:
        team_a, score_a, score_b, team_b = match.groups()
        return f"准确比分是否为{sports_name_cn(team_a)} {score_a}-{score_b} {sports_name_cn(team_b)}"

    if raw.lower().strip("? ") == "exact score: any other score":
        return "准确比分是否为其他比分"

    match = re.search(
        r"Will (.+?) be the Democratic nominee for CO-01\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{person_name_cn(match.group(1))}是否成为科罗拉多州第1选区民主党提名人"

    match = re.search(
        r"Will (.+?) win the 2nd round of the 2026 Peru presidential election(.*?)\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        margin = match.group(2).strip()
        margin_cn = f"（条件：{margin.removeprefix('by ').strip()}）" if margin else ""
        return f"{person_name_cn(match.group(1))} 是否赢得2026年秘鲁总统选举第二轮{margin_cn}"

    match = re.search(
        r"Will (.+?) win the 2026 New York 8th District.*\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{person_name_cn(match.group(1))} 是否赢得2026年纽约第8选区选举"

    match = re.search(
        r"Will (.+?) win the 2026 Makerfield by-election\??$",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        return f"{person_name_cn(match.group(1))} 是否赢得2026年{sports_name_cn('makerfield')}补选"

    if "bitcoin above" in lower:
        return f"比特币价格是否高于 {outcome}"
    if "ethereum above" in lower:
        return f"以太坊价格是否高于 {outcome}"
    if "will the price of bitcoin be above" in lower:
        return localized
    if "will bitcoin reach" in lower:
        return localized
    if "will ethereum reach" in lower or "will the price of ethereum be above" in lower:
        return localized
    if "us announces new iran agreement" in lower or "ceasefire extension" in lower:
        return localized
    if "us x iran permanent peace deal" in lower:
        return localized
    if "elon musk" in lower and "tweets" in lower:
        if outcome != "该结果":
            return f"{localized}：是否落在 {outcome}"
        return localized
    if "counter-strike" in lower:
        return f"{event_context_cn(signal)}：{outcome}"
    if "lpl" in lower or "league of legends" in lower or "lol:" in lower:
        return f"{event_context_cn(signal)}：{outcome}"
    if "any player penta kill" in lower:
        return f"{event_context_cn(signal)}：是否出现五杀"
    if "winner" in lower and signal.category.key == "sports":
        return f"{event_context_cn(signal)}：{outcome}"
    if "fomc" in lower:
        return localized
    return localized


LLM_SYSTEM_PROMPT = (
    "你把 Polymarket 预测市场的英文标题翻译成简体中文短标题。"
    "规则：1) 保留疑问命题语气，用「是否」句式，不要加问号；"
    "2) 每条不超过 30 个汉字；3) 人名、球队名、机构名一律译成中文常用译名；"
    "4) 只有这些缩写可以保留英文：{allowed}；5) 不要解释、不要加引号。"
    '只输出 JSON 数组，元素形如 {{"i": 0, "cn": "……"}}，数量和顺序与输入一致。'
)


def llm_settings(env: dict[str, str]) -> dict[str, str] | None:
    """Read the OpenAI-compatible endpoint from .env. Missing key or URL means "stay offline"."""
    api_key = (env.get("CLIPROXY_API_KEY") or "").strip()
    base = (env.get("CLIPROXY_BASE_URL") or "").strip().rstrip("/")
    if not api_key or not base:
        return None
    return {
        "base": base,
        "api_key": api_key,
        "model": (env.get("CLIPROXY_TRANSLATE_MODEL") or LLM_MODEL_DEFAULT).strip(),
    }


def translation_cache_key(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def load_translation_cache() -> dict[str, dict[str, Any]]:
    if not TRANSLATION_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(TRANSLATION_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def prune_translation_cache(
    cache: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Drop stale entries, then the oldest, so the file cannot grow without bound."""
    now = now or now_utc()
    cutoff = now - timedelta(days=TRANSLATION_CACHE_TTL_DAYS)
    kept: list[tuple[datetime, str, dict[str, Any]]] = []
    for key, entry in cache.items():
        if not isinstance(entry, dict) or not entry.get("cn"):
            continue
        stamp = parse_datetime(entry.get("ts")) or now
        if stamp < cutoff:
            continue
        kept.append((stamp, key, entry))
    kept.sort(key=lambda row: row[0], reverse=True)
    return {key: entry for _stamp, key, entry in kept[:TRANSLATION_CACHE_MAX_ENTRIES]}


def save_translation_cache(cache: dict[str, dict[str, Any]]) -> None:
    write_json(TRANSLATION_CACHE_PATH, prune_translation_cache(cache))


def parse_llm_translation_payload(content: str) -> dict[int, str]:
    """Pull {index: translation} out of a model reply that may be fenced."""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        rows = json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return {}
    result: dict[int, str] = {}
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        translated = str(row.get("cn") or "").strip()
        if translated:
            result[index] = translated
    return result


def request_llm_translations(titles: list[str], settings: dict[str, str]) -> dict[int, str]:
    """One batched call. Any failure returns {} and the rule translation stands."""
    if not titles:
        return {}
    payload = {
        "model": settings["model"],
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": LLM_SYSTEM_PROMPT.format(
                    allowed=", ".join(sorted(TRANSLATION_ALLOWED_IDENTIFIERS)).upper()
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    [{"i": index, "en": title[:LLM_MAX_TITLE_CHARS]} for index, title in enumerate(titles)],
                    ensure_ascii=False,
                ),
            },
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{settings['base']}/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=LLM_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        return {}
    return parse_llm_translation_payload(str(choices[0].get("message", {}).get("content") or ""))


def llm_translation_acceptable(candidate: str, english_title: str) -> bool:
    """Reject anything that is not a plausible short Chinese title.

    A model that echoes the English back, answers the question, or emits an
    apology must not reach the card, so acceptance is checked here rather than
    trusted from the response shape.
    """
    text = candidate.strip()
    if not text or len(text) > 60:
        return False
    if not re.search(r"[㐀-鿿]", text):
        return False
    if text.lower() == english_title.strip().lower():
        return False
    cjk = len(re.findall(r"[㐀-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return cjk >= max(4, latin // 2)


def apply_llm_translations(
    signals: list[Signal],
    enabled: bool = True,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Give poorly rule-translated titles a second chance via the LLM.

    Display-only by construction: scores were already computed and are left
    untouched, so a better translation can promote an item past the display and
    digest quality gates but can never reorder the ranking.
    """
    now = now or now_utc()
    stats = {"attempted": 0, "cache_hits": 0, "llm_applied": 0, "rejected": 0, "status": "skipped"}
    pending = [
        signal
        for signal in signals
        if signal.translation_status != "ok"
        # Categories without a display section can never surface anywhere, so
        # spending LLM batch slots on their titles starves displayable ones.
        and signal.category.key in CATEGORY_TO_SECTION
    ]
    if not enabled or not pending:
        stats["status"] = "disabled" if not enabled else "not_needed"
        return stats

    settings = llm_settings(env if env is not None else load_env())
    cache = load_translation_cache()
    uncached: list[Signal] = []
    for signal in pending:
        english = signal.market_title or signal.title
        entry = cache.get(translation_cache_key(english))
        candidate = str((entry or {}).get("cn") or "")
        if candidate and llm_translation_acceptable(candidate, english):
            adopt_llm_translation(signal, candidate)
            stats["cache_hits"] += 1
        else:
            uncached.append(signal)

    if settings is None:
        stats["status"] = "no_credential" if uncached else "cache_only"
        return stats

    batch = sorted(uncached, key=lambda signal: signal.score, reverse=True)[:LLM_MAX_TITLES]
    if not batch:
        stats["status"] = "cache_only"
        return stats

    stats["attempted"] = len(batch)
    stamp = now.isoformat(timespec="seconds")
    errors: list[str] = []
    for start in range(0, len(batch), LLM_BATCH_SIZE):
        chunk = batch[start : start + LLM_BATCH_SIZE]
        titles = [signal.market_title or signal.title for signal in chunk]
        try:
            translations = request_llm_translations(titles, settings)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        for index, signal in enumerate(chunk):
            candidate = translations.get(index, "").strip()
            english = titles[index]
            if not candidate or not llm_translation_acceptable(candidate, english):
                stats["rejected"] += 1
                continue
            adopt_llm_translation(signal, candidate)
            cache[translation_cache_key(english)] = {
                "cn": candidate,
                "ts": stamp,
                "model": settings["model"],
            }
            stats["llm_applied"] += 1

    save_translation_cache(cache)
    if errors:
        stats["errors"] = errors[:4]
        stats["status"] = "error" if not stats["llm_applied"] else "partial"
    else:
        stats["status"] = "ok"
    return stats


def adopt_llm_translation(signal: Signal, candidate: str) -> None:
    signal.title_cn_override = candidate
    signal.translation_engine = "llm"
    # Re-graded against the same bar the rule translator faces, so the front
    # page never gets easier just because a different engine wrote the title.
    signal.translation_status = title_translation_status(candidate)


def primary_probability(signal: Signal) -> float | None:
    for label, probability in signal.outcome_prices:
        if label.lower().strip() in {"yes", "y"}:
            return probability
    return signal.probability


def story_key(signal: Signal) -> str:
    raw = f"{signal.market_title} {signal.title}".lower()
    section_key = CATEGORY_TO_SECTION.get(signal.category.key, signal.category.key)

    explicit_rules: tuple[tuple[str, str], ...] = (
        (r"\bus x iran diplomatic meeting\b|\bnext diplomatic us-iran meeting\b", "us_iran_diplomatic_meeting"),
        (r"\btext of the us-iran agreement\b|\bus-iran agreement.*text\b", "us_iran_agreement_text"),
        (r"\bus x iran permanent peace deal\b|\bus and iran sign an agreement\b", "us_iran_agreement"),
        (r"\bisrael x iran permanent peace deal\b", "israel_iran_peace_deal"),
        (r"\bunfreeze iranian assets\b|\birani?an enrichment\b|\birani?an oil sanction relief\b", "iran_negotiation_terms"),
        (r"\bcrude oil\b|\bwti\b|\bbrent\b", "crude_oil_price_levels"),
        (r"\bhormuz traffic returns to normal\b|\bstrait of hormuz traffic returns to normal\b", "hormuz_shipping"),
        (r"\bstrait of hormuz\b|\btargets shipping\b|\bshipping by\b", "hormuz_security_incident"),
        (r"\bfed\b|\bfomc\b|\brate cuts?\b|\brate hikes?\b|\binterest rates?\b", "fed_rate_path"),
        (r"\bbitcoin\b|\bbtc\b", "btc_price_levels"),
        (r"\bethereum\b|\beth\b", "eth_price_levels"),
        (r"\bsolana\b|\bsol\b", "sol_price_levels"),
        (r"\belon musk\b.*\btweets?\b", "elon_musk_tweet_count"),
        (r"\bclaude\b|\banthropic\b|\bopenai\b|\bgpt-[0-9.]+\b|\bchatgpt\b", "ai_product_timeline"),
        (r"\bstarmer out\b", "uk_leadership_stability"),
        (r"\bperu(?:vian)? presidential election\b|\bfujimori\b", "peru_presidential_election"),
        (r"\bmakerfield by-election\b", "makerfield_by_election"),
        (r"\bexact score\b", "match_exact_score"),
        (r"\bgames total\b|\bo/u\b|\bboth teams slay a dragon\b", "game_market_props"),
    )
    for pattern, key in explicit_rules:
        if re.search(pattern, raw, flags=re.IGNORECASE):
            return f"{section_key}:{key}"

    world_cup_match = re.search(r"win group ([a-z]) in the 2026 fifa world cup", raw, flags=re.IGNORECASE)
    if world_cup_match:
        return f"{section_key}:world_cup_group_{world_cup_match.group(1).lower()}"

    match_winner = re.search(r"will (.+?) win on (\d{4}-\d{2}-\d{2})", raw, flags=re.IGNORECASE)
    if match_winner:
        event_title = re.sub(r"[^a-z0-9]+", "_", signal.title.lower()).strip("_")
        return f"{section_key}:match_winner_{match_winner.group(2)}_{event_title[:40]}"

    normalized = re.sub(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:,\s*\d{4})?",
        " date ",
        raw,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " date ", normalized)
    normalized = re.sub(r"\$?[0-9][0-9,.]*(?:\.[0-9]+)?[tbm]?\b", " number ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b\d+\s*-\s*\d+\b", " range ", normalized)
    normalized = re.sub(r"\b(will|the|a|an|by|before|after|on|in|from|to|be|is|are|of|for)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return f"{section_key}:{normalized[:72] or signal.market_id}"


def story_title_cn(item: ReportItem) -> str:
    key = item.story_key
    if key.endswith(":us_iran_diplomatic_meeting"):
        return "美国与伊朗外交会谈"
    if key.endswith(":us_iran_agreement_text"):
        return "美国-伊朗协议文本发布"
    if key.endswith(":us_iran_agreement"):
        return "美国与伊朗协议进展"
    if key.endswith(":israel_iran_peace_deal"):
        return "以色列与伊朗和平协议"
    if key.endswith(":iran_negotiation_terms"):
        if len(item.signals) == 1:
            return market_title_cn(item.representative)
        return "伊朗谈判条件变化"
    if key.endswith(":crude_oil_price_levels"):
        return "原油价格关键价位"
    if key.endswith(":hormuz_shipping"):
        return "霍尔木兹海峡航运恢复"
    if key.endswith(":hormuz_security_incident"):
        if len(item.signals) == 1:
            return market_title_cn(item.representative)
        return "霍尔木兹海峡安全事件"
    if key.endswith(":fed_rate_path"):
        return "美联储利率路径"
    if key.endswith(":btc_price_levels"):
        return "比特币关键价位"
    if key.endswith(":eth_price_levels"):
        return "以太坊关键价位"
    if key.endswith(":sol_price_levels"):
        return "Solana 关键价位"
    if key.endswith(":elon_musk_tweet_count"):
        return "马斯克发帖数量区间"
    if key.endswith(":ai_product_timeline"):
        return "AI 产品发布与服务恢复"
    if key.endswith(":uk_leadership_stability"):
        return "英国首相斯塔默去留"
    if key.endswith(":peru_presidential_election"):
        return "秘鲁总统选举第二轮"
    if key.endswith(":makerfield_by_election"):
        return "梅克菲尔德补选"
    if key.endswith(":match_exact_score"):
        return "体育赛事准确比分"
    if key.endswith(":game_market_props"):
        return "电竞赛事局内盘口"
    return market_title_cn(item.representative)


def story_option_label(signal: Signal, key: str) -> str:
    raw = signal.market_title or signal.title
    lower = raw.lower()
    if key.endswith(":us_iran_diplomatic_meeting"):
        date_match = re.search(r"\bby\s+(.+?)\??$", raw, flags=re.IGNORECASE)
        if date_match:
            return f"{date_phrase_cn(date_match.group(1))}前"
        place_match = re.search(r"\bin\s+(.+?)\??$", raw, flags=re.IGNORECASE)
        if place_match:
            return f"{sports_name_cn(place_match.group(1))}举行"
    if key.endswith(":fed_rate_path"):
        if "rate hike" in lower:
            return "2026年加息"
        if "no fed rate cuts" in lower or "no rate cuts" in lower:
            return "2026年不降息"
        meeting_match = re.search(r"after the (.+?) meeting", raw, flags=re.IGNORECASE)
        if meeting_match:
            return f"{date_phrase_cn(meeting_match.group(1))}会议后不变"
    if key.endswith(":elon_musk_tweet_count"):
        range_match = re.search(r"\b([0-9]+-[0-9]+)\s+tweets?\b", raw, flags=re.IGNORECASE)
        if range_match:
            return range_match.group(1)
        if signal.outcome and translate_outcome(signal.outcome) != "该结果":
            return translate_outcome(signal.outcome)
    if key.endswith(":btc_price_levels") or key.endswith(":eth_price_levels") or key.endswith(":sol_price_levels"):
        title = market_title_cn(signal)
        title = re.sub(r"^(比特币|以太坊|Solana)在", "", title)
        title = re.sub(r"^(比特币|以太坊|Solana)", "", title)
        return clean_text(title, 24)
    if key.endswith(":crude_oil_price_levels"):
        return clean_text(market_title_cn(signal).replace("原油在", ""), 24)
    if key.endswith(":hormuz_security_incident"):
        return clean_text(market_title_cn(signal), 30)
    if key.startswith("sports_culture:world_cup_group_"):
        team_match = re.search(r"Will (.+?) win Group", raw, flags=re.IGNORECASE)
        if team_match:
            return sports_name_cn(team_match.group(1))
    if key.endswith(":game_market_props") or key.endswith(":match_exact_score"):
        return clean_text(market_title_cn(signal), 24)
    if key.endswith(":ai_product_timeline"):
        if "claude" in lower:
            return "Claude 服务恢复"
        gpt_match = re.search(r"GPT-([0-9.]+)", raw, flags=re.IGNORECASE)
        if gpt_match:
            return f"GPT-{gpt_match.group(1)} 发布"
    title = market_title_cn(signal)
    return clean_text(title, 26)


def format_story_probabilities(item: ReportItem, max_options: int = 4) -> str:
    if len(item.signals) == 1:
        return format_options_cn(item.representative)

    parts: list[str] = []
    used_labels: set[str] = set()
    omitted_unique = 0
    for signal in item.signals:
        label = story_option_label(signal, item.story_key)
        if label in used_labels:
            continue
        if len(parts) >= max_options:
            omitted_unique += 1
            used_labels.add(label)
            continue
        probability = primary_probability(signal)
        parts.append(f"{escape(label)} 是 <code>{format_prob_cn(probability)}</code>")
        used_labels.add(label)
    if omitted_unique:
        parts.append(f"另{omitted_unique}项未列")
    return "概率：" + "｜".join(parts) if parts else "概率：暂无可用概率"


def section_report_items(
    signals: list[Signal],
    category_keys: tuple[str, ...],
    limit: int = BASE_SECTION_DISPLAY_ITEMS,
    ok_score_floor: float = DISPLAY_SCORE,
    review_score_floor: float = DISPLAY_FALLBACK_SCORE,
) -> list[ReportItem]:
    groups: dict[str, list[Signal]] = {}
    for signal in signals:
        min_score = ok_score_floor if signal.translation_status == "ok" else review_score_floor
        if (
            signal.category.key not in category_keys
            or signal.eligibility_status != "eligible"
            or signal.translation_status not in DISPLAY_TRANSLATION_STATUSES
            or signal.score < min_score
        ):
            continue
        key = story_key(signal)
        groups.setdefault(key, []).append(signal)

    items: list[ReportItem] = []
    for key, group_signals in groups.items():
        ordered = tuple(sorted(group_signals, key=lambda item: item.score, reverse=True))
        total_volume = sum(signal.volume_24h for signal in ordered)
        item = ReportItem(
            story_key=key,
            title="",
            signals=ordered,
            score=max(signal.score for signal in ordered),
            total_volume_24h=total_volume,
        )
        items.append(
            ReportItem(
                story_key=key,
                title=story_title_cn(item),
                signals=ordered,
                score=item.score,
                total_volume_24h=total_volume,
            )
        )
    return sorted(items, key=lambda item: (item.score, item.total_volume_24h), reverse=True)[:limit]


def section_items(signals: list[Signal], category_keys: tuple[str, ...]) -> list[Signal]:
    return [item.representative for item in section_report_items(signals, category_keys)]


def display_slot_count() -> int:
    return TARGET_DISPLAY_ITEMS


def report_line(item: ReportItem, index: int, color_block: str) -> str:
    signal = item.representative
    title = escape(clean_text(item.title, 54))
    number_badge = NUMBER_BADGES[index - 1] if index <= len(NUMBER_BADGES) else f"{index}."
    return (
        f"{number_badge} {color_block} 事件：{title}\n"
        f"   {format_story_probabilities(item)}\n"
        f"   变化：24小时 <code>{format_delta_cn(signal.delta_24h)}</code>｜"
        f"成交 <code>{format_money_cn(item.total_volume_24h)}</code>"
    )


def placeholder_line(index: int, color_block: str) -> str:
    number_badge = NUMBER_BADGES[index - 1] if index <= len(NUMBER_BADGES) else f"{index}."
    return f"{number_badge} {color_block} 暂无新增强信号\n   🔻 保持观察｜候选不足三条｜不做交易动作"


def extract_tags(event: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for tag in event.get("tags") or []:
        if isinstance(tag, dict):
            out.extend(str(tag.get(key, "")) for key in ("label", "slug") if tag.get(key))
    for series in event.get("series") or []:
        if isinstance(series, dict):
            out.extend(str(series.get(key, "")) for key in ("title", "slug") if series.get(key))
    sport = event.get("sport")
    if isinstance(sport, dict):
        out.extend(str(sport.get(key, "")) for key in ("sport", "series") if sport.get(key))
    return out


def event_search_text(event: dict[str, Any]) -> str:
    parts = [
        str(event.get("title", "")),
        str(event.get("ticker", "")),
        str(event.get("slug", "")),
        str(event.get("description", "")),
        " ".join(extract_tags(event)),
    ]
    metadata = event.get("eventMetadata") or {}
    if isinstance(metadata, dict):
        parts.append(str(metadata.get("context_description", "")))
    for market in event.get("markets") or []:
        if isinstance(market, dict):
            parts.append(str(market.get("question", "")))
            parts.append(str(market.get("groupItemTitle", "")))
            parts.append(str(market.get("sportsMarketType", "")))
    return " ".join(parts).lower()


def classify_event(event: dict[str, Any]) -> Category:
    text = event_search_text(event)
    best_category = OTHER_CATEGORY
    best_score = 0.0
    for category in CATEGORIES:
        score = 0.0
        for keyword in category.keywords:
            if keyword in text:
                score += 1.0 + min(len(keyword) / 20.0, 1.0)
        if score > best_score:
            best_score = score
            best_category = category
    return best_category


def classify_candidate(event: dict[str, Any], market: dict[str, Any]) -> tuple[Category, str]:
    market_parts = [
        str(market.get("question", "")),
        str(market.get("groupItemTitle", "")),
        str(market.get("sportsMarketType", "")),
        str(market.get("marketType", "")),
    ]
    event_parts = [
        str(event.get("title", "")),
        str(event.get("ticker", "")),
        str(event.get("slug", "")),
        " ".join(extract_tags(event)),
    ]
    market_text = " ".join(market_parts).lower()
    event_text = " ".join(event_parts).lower()
    combined = f"{market_text} {event_text}"

    if re.search(
        r"\b(newsom|federally charged|foreign secretary|supreme court|election|president|senate|house|district|by-election|primary|minister|secretary|retirement|parliamentary|pm|starmer|fujimori)\b",
        combined,
    ):
        return next(category for category in CATEGORIES if category.key == "politics_policy"), "market_politics_rule"
    if re.search(r"\b(crude|oil|wti|brent|gold|silver|xauusd|xagusd|hormuz traffic|fed|fomc|rate cut|interest rate|treasury|inflation|cpi|ppi|gdp|recession)\b", market_text):
        return next(category for category in CATEGORIES if category.key == "macro_finance"), "market_macro_rule"
    if re.search(r"\b(hormuz|strait of hormuz|ships? transit|vessels? transit|targets shipping)\b", market_text):
        return next(category for category in CATEGORIES if category.key == "geopolitics"), "market_hormuz_geopolitics_rule"
    if re.search(r"\b(bitcoin|btc|ethereum|eth|solana|crypto|coinbase|binance|token)\b", market_text):
        return next(category for category in CATEGORIES if category.key == "crypto_web3"), "market_crypto_rule"
    if re.search(r"\b(nba|nfl|mlb|nhl|soccer|football|fifa|world cup|knockout stages|dota|lol|league of legends|counter-strike|game|games total|exact score|o/u|slay a dragon)\b", combined):
        return next(category for category in CATEGORIES if category.key == "sports"), "market_sports_rule"
    if re.search(r"\b(iran|iranian|israel|russia|ukraine|ceasefire|peace deal|agreement|sanction|military|strike|war|diplomatic)\b", market_text):
        return next(category for category in CATEGORIES if category.key == "geopolitics"), "market_geopolitics_rule"
    if re.search(r"\b(spacex|tesla|google|alphabet|nvidia|apple|microsoft|amazon|meta|elon musk|valuation|tweets|claude|anthropic|openai|chatgpt|gpt-[0-9.]+)\b", combined):
        return next(category for category in CATEGORIES if category.key == "tech_business"), "market_tech_rule"

    best_category = OTHER_CATEGORY
    best_score = 0.0
    for category in CATEGORIES:
        score = 0.0
        for keyword in category.keywords:
            if keyword in market_text:
                score += 2.0 + min(len(keyword) / 20.0, 1.0)
            elif keyword in event_text:
                score += 0.75 + min(len(keyword) / 30.0, 0.5)
        if score > best_score:
            best_score = score
            best_category = category
    return best_category, f"keyword_score:{best_score:.1f}" if best_score else "fallback_other"


def market_base_open(market: dict[str, Any]) -> bool:
    if not is_true(market.get("active"), default=True):
        return False
    if is_true(market.get("closed"), default=False):
        return False
    if is_true(market.get("archived"), default=False):
        return False
    if market.get("acceptingOrders") is not None and is_false(market.get("acceptingOrders")):
        return False
    return True


def top_market(event: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    now = now or now_utc()
    markets = [market for market in event.get("markets") or [] if isinstance(market, dict)]
    open_markets = [
        market
        for market in markets
        if market_base_open(market)
    ]
    if not open_markets:
        return None

    future_markets: list[dict[str, Any]] = []
    fallback_markets: list[dict[str, Any]] = []
    for market in open_markets:
        deadline, _source = effective_deadline(event, market)
        hours = ttl_hours(deadline, now)
        if hours is None:
            fallback_markets.append(market)
        elif hours > DEADLINE_GRACE_HOURS:
            future_markets.append(market)
    candidate_markets = future_markets or fallback_markets
    if not candidate_markets:
        return sorted(open_markets, key=lambda item: fnum(item.get("volume24hr") or item.get("volume24hrClob")), reverse=True)[0]

    def sort_key(market: dict[str, Any]) -> tuple[float, float, float]:
        move = max(
            abs(fnum(market.get("oneHourPriceChange"))),
            abs(fnum(market.get("oneDayPriceChange"))),
            abs(fnum(market.get("oneWeekPriceChange")) * 0.35),
        )
        return (
            move,
            fnum(market.get("volume24hr") or market.get("volume24hrClob")),
            fnum(market.get("liquidity") or market.get("liquidityNum")),
        )

    return sorted(candidate_markets, key=sort_key, reverse=True)[0]


def probability_for_market(market: dict[str, Any]) -> tuple[str, float | None]:
    outcome = str(market.get("groupItemTitle") or "Yes")
    prices = parse_json_list(market.get("outcomePrices"))
    outcomes = parse_json_list(market.get("outcomes"))
    if outcomes:
        outcome = str(market.get("groupItemTitle") or outcomes[0])
    if prices:
        return outcome, fnum(prices[0], default=None)  # type: ignore[arg-type]
    for key in ("lastTradePrice", "bestAsk", "bestBid"):
        value = market.get(key)
        if value not in (None, ""):
            return outcome, fnum(value, default=None)  # type: ignore[arg-type]
    return outcome, None


def outcome_prices_for_market(market: dict[str, Any]) -> tuple[tuple[str, float], ...]:
    outcomes = parse_json_list(market.get("outcomes"))
    prices = parse_json_list(market.get("outcomePrices"))
    pairs: list[tuple[str, float]] = []
    for outcome, price in zip(outcomes, prices):
        probability = fnum(price, default=None)  # type: ignore[arg-type]
        if probability is None:
            continue
        pairs.append((str(outcome), probability))
    if pairs:
        return tuple(pairs)

    outcome, probability = probability_for_market(market)
    if probability is None:
        return ()
    return ((outcome, probability),)


def days_until(value: str | None) -> float | None:
    dt = parse_datetime(value)
    if dt is None:
        return None
    return (dt - now_utc()).total_seconds() / 86_400


def market_volume_24h(market: dict[str, Any]) -> float:
    # `volume` / `volumeNum` are cumulative lifetime totals; mixing them in here
    # let dormant markets (Taiwan 2026: $0.03M in 24h, $39M lifetime) read as hot.
    return max(
        fnum(market.get("volume24hr")),
        fnum(market.get("volume24hrClob")),
    )


def market_liquidity(market: dict[str, Any]) -> float:
    return max(
        fnum(market.get("liquidity")),
        fnum(market.get("liquidityNum")),
        fnum(market.get("liquidityClob")),
    )


def eligibility_for_candidate(
    event: dict[str, Any],
    market: dict[str, Any],
    now: datetime,
    translation_status: str = "ok",
) -> tuple[str, str | None, datetime | None, str | None, float | None]:
    deadline, deadline_source = effective_deadline(event, market)
    hours = ttl_hours(deadline, now)
    if not is_true(event.get("active"), default=True):
        return "rejected", "event_inactive", deadline, deadline_source, hours
    if is_true(event.get("closed"), default=False):
        return "rejected", "event_closed", deadline, deadline_source, hours
    if is_true(event.get("archived"), default=False):
        return "rejected", "event_archived", deadline, deadline_source, hours
    if not market_base_open(market):
        return "rejected", "market_not_open", deadline, deadline_source, hours
    if hours is not None and hours <= DEADLINE_GRACE_HOURS:
        return "rejected", "deadline_passed", deadline, deadline_source, hours
    return "eligible", None, deadline, deadline_source, hours


def score_signal(
    event: dict[str, Any],
    market: dict[str, Any],
    category: Category,
    ttl: float | None,
    translation_status: str,
    source_status: str,
) -> tuple[float, str, dict[str, float]]:
    volume = market_volume_24h(market)
    liquidity = market_liquidity(market)
    move_1h = abs(fnum(market.get("oneHourPriceChange")))
    move_1d = abs(fnum(market.get("oneDayPriceChange")))
    move_1w = abs(fnum(market.get("oneWeekPriceChange"))) * 0.35
    max_move = max(move_1h, move_1d, move_1w)
    _outcome, probability = probability_for_market(market)

    volume_score = min(28.0, math.log10(max(volume, 1.0)) * 4.8)
    liquidity_score = min(12.0, math.log10(max(liquidity, 1.0)) * 2.0)
    move_score = min(32.0, max_move * 300.0)
    # A 4-point odds move at 97% is resolution drift, not news; the same move
    # at 50% is a real swing. Damp tail-probability moves unless they are big
    # enough (>=8pt) to be news at any price level.
    move_tail_damped = 0.0
    if probability is not None and (probability < 0.05 or probability > 0.95) and max_move < 0.08:
        move_score *= 0.6
        move_tail_damped = 1.0

    ttl_score = 2.0
    if ttl is not None:
        if 6 < ttl <= 24:
            ttl_score = 10.0
        elif 24 < ttl <= 72:
            ttl_score = 8.0
        elif 72 < ttl <= 168:
            ttl_score = 5.0
        elif 0 < ttl <= 6:
            ttl_score = 4.0
        elif ttl > 168:
            ttl_score = 3.0

    created_days = days_until(event.get("createdAt") or event.get("creationDate"))
    freshness_score = 0.0
    if created_days is not None:
        age_days = -created_days
        if 0 <= age_days <= 1:
            freshness_score = 8.0
        elif 0 <= age_days <= 3:
            freshness_score = 4.0

    probability_penalty = 1.0
    if probability is not None:
        if probability >= 0.995 or probability <= 0.005:
            probability_penalty = 0.45
        elif (probability >= 0.98 or probability <= 0.02) and max_move < 0.04:
            probability_penalty = 0.7

    volume_penalty = 0.75 if volume < MIN_VOLUME_24H else 1.0
    translation_penalty = 0.72 if translation_status != "ok" else 1.0
    if source_status == "confirmed":
        source_penalty = 1.0
    elif source_status.startswith("markets"):
        source_penalty = 0.95
    else:
        source_penalty = 0.9

    base_score = volume_score + liquidity_score + move_score + ttl_score + freshness_score
    score = base_score * category.weight * probability_penalty * volume_penalty * translation_penalty * source_penalty
    score = min(100.0, score)

    reasons: list[str] = []
    if max_move >= 0.08:
        reasons.append("large odds move")
    elif max_move >= 0.04:
        reasons.append("odds move")
    if volume >= 1_000_000:
        reasons.append("high 24h volume")
    elif volume >= 100_000:
        reasons.append("active market")
    if ttl_score >= 8:
        reasons.append("near event window")
    if freshness_score >= 4:
        reasons.append("new market")
    if volume < MIN_VOLUME_24H:
        reasons.append("low market volume")
    if probability_penalty < 1:
        reasons.append("near-certain odds")
    if source_status != "confirmed":
        reasons.append(source_status)
    if not reasons:
        reasons.append("category heat")
    breakdown = {
        "volume": round(volume_score, 2),
        "liquidity": round(liquidity_score, 2),
        "move": round(move_score, 2),
        "move_tail_damped": move_tail_damped,
        "ttl": round(ttl_score, 2),
        "freshness": round(freshness_score, 2),
        "category_weight": round(category.weight, 2),
        "probability_penalty": round(probability_penalty, 2),
        "volume_penalty": round(volume_penalty, 2),
        "translation_penalty": round(translation_penalty, 2),
        "source_penalty": round(source_penalty, 2),
    }
    return score, ", ".join(reasons), breakdown


def rejection_to_dict(
    event: dict[str, Any],
    market: dict[str, Any] | None,
    reason: str,
    source_status: str,
    now: datetime,
) -> dict[str, Any]:
    market = market or {}
    deadline, deadline_source = effective_deadline(event, market)
    hours = ttl_hours(deadline, now)
    return {
        "event_id": str(event.get("id") or event.get("slug") or ""),
        "market_id": str(market.get("id") or market.get("conditionId") or market.get("slug") or ""),
        "title": str(event.get("title") or ""),
        "market_title": str(market.get("question") or ""),
        "reject_reason": reason,
        "source_status": source_status,
        "deadline_source": deadline_source,
        "effective_deadline_utc": iso_utc(deadline),
        "effective_deadline_sgt": sgt_datetime_label(deadline),
        "ttl_hours": round(hours, 2) if hours is not None else None,
    }


def build_signal(
    event: dict[str, Any],
    market: dict[str, Any],
    source_status: str,
    now: datetime,
) -> tuple[Signal | None, dict[str, Any] | None]:
    outcome, probability = probability_for_market(market)
    event_id = str(event.get("id") or event.get("slug") or market.get("conditionId") or market.get("id") or "")
    market_id = str(market.get("id") or market.get("conditionId") or market.get("slug") or event_id)
    slug = str(event.get("slug") or market.get("slug") or "")
    preview_signal = Signal(
        event_id=event_id,
        market_id=market_id,
        category=OTHER_CATEGORY,
        title=str(event.get("title") or market.get("question") or slug),
        market_title=str(market.get("question") or event.get("title") or slug),
        outcome=outcome,
        outcome_prices=outcome_prices_for_market(market),
        probability=probability,
        delta_1h=fnum(market.get("oneHourPriceChange"), default=None),  # type: ignore[arg-type]
        delta_24h=fnum(market.get("oneDayPriceChange"), default=None),  # type: ignore[arg-type]
        delta_1w=fnum(market.get("oneWeekPriceChange"), default=None),  # type: ignore[arg-type]
        volume_24h=market_volume_24h(market),
        liquidity=market_liquidity(market),
        end_date=field_value(market, "endDateIso", "endDate") or field_value(event, "endDateIso", "endDate"),
        effective_deadline_utc=None,
        effective_deadline_sgt=None,
        deadline_source=None,
        ttl_hours=None,
        eligibility_status="preview",
        reject_reason=None,
        source_status=source_status,
        category_reason="preview",
        translation_status="preview",
        score_breakdown={},
        event_url=f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/",
        score=0.0,
        reason="preview",
    )
    title_cn = market_title_cn(preview_signal)
    translation_status = title_translation_status(title_cn)
    eligibility_status, reject_reason, deadline, deadline_source, hours = eligibility_for_candidate(
        event,
        market,
        now,
        translation_status=translation_status,
    )
    if eligibility_status != "eligible":
        rejected = rejection_to_dict(event, market, reject_reason or "not_eligible", source_status, now)
        rejected["translation_status"] = translation_status
        rejected["rendered_title"] = title_cn
        return None, rejected

    category, category_reason = classify_candidate(event, market)
    score, reason, breakdown = score_signal(event, market, category, hours, translation_status, source_status)
    event_id = str(event.get("id") or event.get("slug") or market.get("conditionId") or "")
    market_id = str(market.get("id") or market.get("conditionId") or market.get("slug") or event_id)
    slug = str(event.get("slug") or market.get("slug") or "")
    return Signal(
        event_id=event_id,
        market_id=market_id,
        category=category,
        title=str(event.get("title") or market.get("question") or slug),
        market_title=str(market.get("question") or event.get("title") or slug),
        outcome=outcome,
        outcome_prices=outcome_prices_for_market(market),
        probability=probability,
        delta_1h=fnum(market.get("oneHourPriceChange"), default=None),  # type: ignore[arg-type]
        delta_24h=fnum(market.get("oneDayPriceChange"), default=None),  # type: ignore[arg-type]
        delta_1w=fnum(market.get("oneWeekPriceChange"), default=None),  # type: ignore[arg-type]
        volume_24h=market_volume_24h(market),
        liquidity=market_liquidity(market),
        end_date=field_value(market, "endDateIso", "endDate") or field_value(event, "endDateIso", "endDate"),
        effective_deadline_utc=iso_utc(deadline),
        effective_deadline_sgt=sgt_datetime_label(deadline),
        deadline_source=deadline_source,
        ttl_hours=round(hours, 2) if hours is not None else None,
        eligibility_status=eligibility_status,
        reject_reason=None,
        source_status=source_status,
        category_reason=category_reason,
        translation_status=translation_status,
        score_breakdown=breakdown,
        event_url=f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/",
        score=score,
        reason=reason,
    ), None


def event_to_signal(event: dict[str, Any], now: datetime | None = None, source_status: str = "events_only") -> Signal | None:
    now = now or now_utc()
    if is_true(event.get("closed"), default=False) or not is_true(event.get("active"), default=True):
        return None
    market = top_market(event, now=now)
    if not market:
        return None
    signal, _rejected = build_signal(event, market, source_status, now)
    return signal


def fetch_events_legacy(limit: int) -> list[dict[str, Any]]:
    pages: list[list[dict[str, Any]]] = []
    for params in (
        # min(limit, 100): Gamma clamps to 100 server-side; asking for more
        # only overstates the coverage this call can actually deliver.
        {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": min(limit, 100)},
        {"active": "true", "closed": "false", "order": "updatedAt", "ascending": "false", "limit": min(limit, 60)},
        {"active": "true", "closed": "false", "order": "createdAt", "ascending": "false", "limit": min(limit, 60)},
    ):
        payload = safe_fetch_json(f"{GAMMA_BASE}/events", params=params)
        if isinstance(payload, list):
            pages.append(payload)
    return unique_records(
        [event for page in pages for event in page if isinstance(event, dict)],
        ("id", "slug", "ticker"),
    )


def fetch_events_keyset(limit: int) -> list[dict[str, Any]]:
    # Gamma clamps every limit to 100 server-side (API changelog 2026-05-14;
    # verified live 2026-08-15: limit=120 and limit=500 both return 100 rows).
    page_limit = 100
    records: list[dict[str, Any]] = []
    for params in (
        {"closed": "false", "order": "volume24hr", "ascending": "false", "limit": page_limit},
        {"closed": "false", "order": "updatedAt", "ascending": "false", "limit": page_limit},
        {"closed": "false", "order": "createdAt", "ascending": "false", "limit": page_limit},
    ):
        records.extend(fetch_keyset_pages("/events/keyset", params, "events", KEYSET_EVENT_PAGES))
    return unique_records(records, ("id", "slug", "ticker"))


def fetch_public_search_events() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    for queries in DISCOVERY_QUERIES_BY_SECTION.values():
        for query in queries:
            if query.lower() in seen_queries:
                continue
            seen_queries.add(query.lower())
            payload = safe_fetch_json(
                f"{GAMMA_BASE}/public-search",
                params={
                    "q": query,
                    "events_status": "active",
                    "limit_per_type": PUBLIC_SEARCH_LIMIT_PER_TYPE,
                    "optimized": "true",
                    "search_tags": "false",
                    "search_profiles": "false",
                },
                timeout=20,
            )
            if not isinstance(payload, dict):
                continue
            events = payload.get("events")
            if isinstance(events, list):
                records.extend(event for event in events if isinstance(event, dict))
    return unique_records(records, ("id", "slug", "ticker"))


def fetch_event_sources(limit: int) -> dict[str, list[dict[str, Any]]]:
    return {
        "events_legacy": timed_fetch("events_legacy", lambda: fetch_events_legacy(limit)),
        "events_keyset": timed_fetch("events_keyset", lambda: fetch_events_keyset(limit)),
        "events_search": timed_fetch("events_search", fetch_public_search_events),
    }


def fetch_events(limit: int) -> list[dict[str, Any]]:
    records = [event for events in fetch_event_sources(limit).values() for event in events]
    return unique_records(records, ("id", "slug", "ticker"))


def fetch_markets_legacy(limit: int, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or now_utc()
    end_min = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    end_max = (now + timedelta(days=MARKET_LOOKAHEAD_DAYS)).isoformat(timespec="seconds").replace("+00:00", "Z")
    pages: list[list[dict[str, Any]]] = []
    for params in (
        {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            # Gamma clamps to 100 server-side; see fetch_events_legacy.
            "limit": min(limit, 100),
            "end_date_min": end_min,
            "end_date_max": end_max,
        },
        {
            "active": "true",
            "closed": "false",
            "order": "updatedAt",
            "ascending": "false",
            "limit": min(limit, 80),
            "end_date_min": end_min,
            "end_date_max": end_max,
        },
    ):
        payload = safe_fetch_json(f"{GAMMA_BASE}/markets", params=params)
        if isinstance(payload, list):
            pages.append(payload)
    return unique_records(
        [market for page in pages for market in page if isinstance(market, dict)],
        ("id", "conditionId", "slug"),
    )


def fetch_markets_keyset(limit: int, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or now_utc()
    end_min = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    short_end_max = (now + timedelta(days=MARKET_LOOKAHEAD_DAYS)).isoformat(timespec="seconds").replace("+00:00", "Z")
    extended_end_max = (now + timedelta(days=MARKET_EXTENDED_LOOKAHEAD_DAYS)).isoformat(timespec="seconds").replace("+00:00", "Z")
    page_limit = min(100, max(50, limit))
    records: list[dict[str, Any]] = []
    for params in (
        {
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": page_limit,
            "end_date_min": end_min,
            "end_date_max": short_end_max,
            "include_tag": "true",
        },
        {
            "closed": "false",
            "order": "updatedAt",
            "ascending": "false",
            "limit": page_limit,
            "end_date_min": end_min,
            "end_date_max": short_end_max,
            "include_tag": "true",
        },
        {
            "closed": "false",
            "order": "volumeNum",
            "ascending": "false",
            "limit": page_limit,
            "end_date_min": end_min,
            "end_date_max": extended_end_max,
            "include_tag": "true",
        },
    ):
        records.extend(fetch_keyset_pages("/markets/keyset", params, "markets", KEYSET_MARKET_PAGES))
    return unique_records(records, ("id", "conditionId", "slug"))


def fetch_market_sources(limit: int, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    return {
        "markets_legacy": timed_fetch("markets_legacy", lambda: fetch_markets_legacy(limit, now=now)),
        "markets_keyset": timed_fetch("markets_keyset", lambda: fetch_markets_keyset(limit, now=now)),
    }


def fetch_markets(limit: int, now: datetime | None = None) -> list[dict[str, Any]]:
    records = [market for markets in fetch_market_sources(limit, now=now).values() for market in markets]
    return unique_records(records, ("id", "conditionId", "slug"))


def market_source_index(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for market in markets:
        for key in market_keys(market):
            index[key] = market
    return index


def source_status_for_market(market: dict[str, Any], market_index: dict[str, dict[str, Any]]) -> str:
    return "confirmed" if any(key in market_index for key in market_keys(market)) else "events_only"


def event_from_market(market: dict[str, Any]) -> dict[str, Any]:
    parent_events = market.get("events")
    if isinstance(parent_events, list) and parent_events and isinstance(parent_events[0], dict):
        event = dict(parent_events[0])
        event["markets"] = [market]
        event.setdefault("active", market.get("active", True))
        event.setdefault("closed", market.get("closed", False))
        event.setdefault("archived", market.get("archived", False))
        return event
    return {
        "id": market.get("eventId") or market.get("event_id") or market.get("id") or market.get("slug"),
        "title": market.get("title") or market.get("question") or market.get("slug"),
        "slug": market.get("eventSlug") or market.get("event_slug") or market.get("slug"),
        "active": market.get("active", True),
        "closed": market.get("closed", False),
        "archived": market.get("archived", False),
        "endDate": market.get("endDate"),
        "endDateIso": market.get("endDateIso"),
        "markets": [market],
    }


def assess_source_health(
    event_sources: dict[str, list[dict[str, Any]]],
    market_sources: dict[str, list[dict[str, Any]]],
    raw_candidate_count: int,
) -> dict[str, Any]:
    """Classify a run as ok / degraded / failed from source coverage.

    A single endpoint going dark is tolerable; every endpoint of one kind going
    dark, or no candidates at all, means the radar is blind rather than quiet.
    """
    empty_events = sorted(name for name, records in event_sources.items() if not records)
    empty_markets = sorted(name for name, records in market_sources.items() if not records)
    empty_sources = empty_events + empty_markets
    all_events_down = bool(event_sources) and len(empty_events) == len(event_sources)
    all_markets_down = bool(market_sources) and len(empty_markets) == len(market_sources)

    if raw_candidate_count == 0 or all_events_down or all_markets_down:
        status = "failed"
    elif empty_sources or FETCH_FAILURES:
        status = "degraded"
    else:
        status = "ok"

    reasons: list[str] = []
    if raw_candidate_count == 0:
        reasons.append("no_candidates")
    if all_events_down:
        reasons.append("all_event_sources_empty")
    if all_markets_down:
        reasons.append("all_market_sources_empty")
    if empty_sources:
        reasons.append("empty_sources:" + ",".join(empty_sources))
    if FETCH_FAILURES:
        reasons.append(f"fetch_failures:{len(FETCH_FAILURES)}")

    return {
        "status": status,
        "reasons": reasons,
        "empty_sources": empty_sources,
        "fetch_failure_count": len(FETCH_FAILURES),
        "fetch_failures": list(FETCH_FAILURES[:12]),
        "source_sizes": {
            **{name: len(records) for name, records in event_sources.items()},
            **{name: len(records) for name, records in market_sources.items()},
        },
    }


def collect_signal_result(limit: int, now: datetime | None = None) -> CollectionResult:
    now = now or now_utc()
    reset_fetch_failures()
    event_sources = fetch_event_sources(limit)
    market_sources = fetch_market_sources(limit, now=now)
    future_markets = unique_records(
        [market for markets in market_sources.values() for market in markets],
        ("id", "conditionId", "slug"),
    )
    future_index = market_source_index(future_markets)
    signals: list[Signal] = []
    rejected: list[dict[str, Any]] = []
    seen_market_keys: set[str] = set()
    seen_event_keys: set[str] = set()

    for source_name, source_events in event_sources.items():
        for event in source_events:
            event_key = str(event.get("id") or event.get("slug") or json.dumps(event, sort_keys=True, ensure_ascii=False)[:160])
            if event_key in seen_event_keys:
                continue
            seen_event_keys.add(event_key)
            if is_true(event.get("closed"), default=False) or not is_true(event.get("active"), default=True):
                rejected.append(rejection_to_dict(event, None, "event_not_open", source_name, now))
                continue
            market = top_market(event, now=now)
            if not market:
                rejected.append(rejection_to_dict(event, None, "no_open_market", source_name, now))
                continue
            source_status = source_status_for_market(market, future_index)
            if source_status != "confirmed":
                source_status = source_name
            signal, reject = build_signal(event, market, source_status, now)
            seen_market_keys.update(market_keys(market))
            if signal:
                signals.append(signal)
            elif reject:
                rejected.append(reject)

    for market in future_markets:
        keys = market_keys(market)
        if keys & seen_market_keys:
            continue
        event = event_from_market(market)
        signal, reject = build_signal(event, market, "markets_keyset", now)
        if signal:
            signals.append(signal)
        elif reject:
            rejected.append(reject)

    signals = sorted(signals, key=lambda item: item.score, reverse=True)
    source_counts = {
        **{key: len(value) for key, value in event_sources.items()},
        **{key: len(value) for key, value in market_sources.items()},
        "events": len(seen_event_keys),
        "markets": len(future_markets),
        "eligible": len(signals),
        "rejected": len(rejected),
    }
    raw_candidate_count = source_counts["events"] + source_counts["markets"]
    return CollectionResult(
        signals=signals,
        rejected=rejected,
        raw_candidate_count=raw_candidate_count,
        source_counts=source_counts,
        source_health=assess_source_health(event_sources, market_sources, raw_candidate_count),
    )


def collect_signals(limit: int) -> list[Signal]:
    return collect_signal_result(limit).signals


def display_item_signal(item: dict[str, Any]) -> dict[str, Any]:
    markets = item.get("display_markets")
    if isinstance(markets, list) and markets and isinstance(markets[0], dict):
        return markets[0]
    return {}


def load_display_seen() -> dict[str, dict[str, Any]]:
    payload = read_json(DISPLAY_SEEN_PATH, {})
    if isinstance(payload, dict) and payload:
        return payload

    latest = read_json(LATEST_PATH, {})
    if not isinstance(latest, dict) or latest.get("dry_run"):
        return {}
    timestamp = str(latest.get("timestamp") or "")
    items = latest.get("display_items")
    if not timestamp or not isinstance(items, list):
        return {}

    bootstrapped: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("story_key") or "")
        if not key:
            continue
        signal = display_item_signal(item)
        bootstrapped[key] = {
            "sent_at": timestamp,
            "title": item.get("title"),
            "section": item.get("section"),
            "score": item.get("score"),
            "representative_market_id": item.get("representative_market_id") or signal.get("market_id"),
            "source": "latest_bootstrap",
        }
    return bootstrapped


def parsed_seen_age_hours(record: dict[str, Any], now: datetime | None = None) -> float | None:
    sent_at = str(record.get("sent_at") or "")
    if not sent_at:
        return None
    try:
        sent_dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if sent_dt.tzinfo is None:
        sent_dt = sent_dt.replace(tzinfo=timezone.utc)
    now = now or now_utc()
    return (now - sent_dt.astimezone(timezone.utc)).total_seconds() / 3600


def display_story_recently_seen(
    story: str,
    display_seen: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> bool:
    record = display_seen.get(story)
    if not record:
        return False
    age_hours = parsed_seen_age_hours(record, now=now)
    if age_hours is None:
        return False
    return age_hours < DISPLAY_COOLDOWN_HOURS


def prune_display_seen(
    display_seen: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    pruned: dict[str, dict[str, Any]] = {}
    for key, record in display_seen.items():
        age_hours = parsed_seen_age_hours(record, now=now)
        if age_hours is None or age_hours <= DISPLAY_SEEN_RETENTION_HOURS:
            pruned[key] = record
    return pruned


def mark_digest_sent(digest_items: list[tuple[str, ReportItem]]) -> None:
    display_seen = prune_display_seen(load_display_seen())
    ts = iso_now()
    for section_key, item in digest_items:
        display_seen[item.story_key] = {
            "sent_at": ts,
            "title": item.title,
            "section": section_key,
            "score": round(item.score, 2),
            "representative_market_id": item.representative.market_id,
        }
    write_json(DISPLAY_SEEN_PATH, display_seen)


def load_probe_snapshot() -> dict[str, Any]:
    """Previous run's snapshot: {"ts": iso, "markets": {market_id: {"p": float}}}."""
    if not PROBE_SNAPSHOT_PATH.exists():
        return {}
    try:
        payload = json.loads(PROBE_SNAPSHOT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def probability_delta_since_last_run(
    market_id: str, probability: float | None, previous_markets: dict[str, Any]
) -> float | None:
    if probability is None:
        return None
    previous = previous_markets.get(market_id)
    prior = previous.get("p") if isinstance(previous, dict) else None
    if not isinstance(prior, (int, float)):
        return None
    return round(probability - float(prior), 4)


def write_probe_snapshot(signals: list[Signal], now: datetime) -> None:
    # Rewritten wholesale each run, so markets that leave the eligible set fall
    # out on their own. Observational only: the deltas are reported in
    # latest.json and never feed back into scoring or display selection.
    write_json(
        PROBE_SNAPSHOT_PATH,
        {
            "ts": now.isoformat(timespec="seconds"),
            "markets": {
                signal.market_id: {"p": round(signal.probability, 4)}
                for signal in signals
                if signal.probability is not None
            },
        },
    )


def signal_to_dict(signal: Signal) -> dict[str, Any]:
    return {
        "event_id": signal.event_id,
        "market_id": signal.market_id,
        "story_key": story_key(signal),
        "category": signal.category.key,
        "category_label": signal.category.label,
        "title": signal.title,
        "market_title": signal.market_title,
        "outcome": signal.outcome,
        "outcome_prices": [{"outcome": label, "probability": probability} for label, probability in signal.outcome_prices],
        "probability": signal.probability,
        "delta_1h": signal.delta_1h,
        "delta_24h": signal.delta_24h,
        "delta_1w": signal.delta_1w,
        "volume_24h": signal.volume_24h,
        "liquidity": signal.liquidity,
        "end_date": signal.end_date,
        "effective_deadline_utc": signal.effective_deadline_utc,
        "effective_deadline_sgt": signal.effective_deadline_sgt,
        "deadline_source": signal.deadline_source,
        "ttl_hours": signal.ttl_hours,
        "eligibility_status": signal.eligibility_status,
        "reject_reason": signal.reject_reason,
        "source_status": signal.source_status,
        "category_reason": signal.category_reason,
        "translation_status": signal.translation_status,
        "score_breakdown": signal.score_breakdown,
        "event_url": signal.event_url,
        "score": round(signal.score, 2),
        "reason": signal.reason,
    }


def report_item_to_dict(item: ReportItem, section_key: str) -> dict[str, Any]:
    return {
        "section": section_key,
        "story_key": item.story_key,
        "story_group_size": len(item.signals),
        "title": item.title,
        "score": round(item.score, 2),
        "total_volume_24h": round(item.total_volume_24h, 2),
        "representative_market_id": item.representative.market_id,
        "display_markets": [
            {
                "market_id": signal.market_id,
                "market_title": signal.market_title,
                "rendered_title": market_title_cn(signal),
                "option_label": story_option_label(signal, item.story_key),
                "probability": primary_probability(signal),
                "score": round(signal.score, 2),
            }
            for signal in item.signals[:6]
        ],
    }


def report_items_by_section(
    signals: list[Signal],
    display_seen: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, list[ReportItem]]:
    display_seen = display_seen or {}
    now = now or now_utc()

    def pool(ok_floor: float, review_floor: float) -> dict[str, list[ReportItem]]:
        return {
            section_key: [
                item
                for item in section_report_items(
                    signals,
                    category_keys,
                    limit=SECTION_CANDIDATE_POOL_ITEMS,
                    ok_score_floor=ok_floor,
                    review_score_floor=review_floor,
                )
                if not display_story_recently_seen(item.story_key, display_seen, now=now)
                and item.total_volume_24h >= DIVERSITY_MIN_VOLUME_24H
            ]
            for section_key, _section_label, category_keys in SECTION_DEFS
        }

    # The score floor is real until a section cannot fill its slots. Relaxing it
    # per-thin-section keeps a quiet corner of the market visible without
    # lowering the bar for the sections that had plenty to choose from — the
    # previous "any cooldown history at all" trigger was permanently on, which
    # made DISPLAY_SCORE dead code.
    strict = pool(DISPLAY_SCORE, DISPLAY_FALLBACK_SCORE)
    relaxed = pool(DIVERSITY_DISPLAY_SCORE, DIVERSITY_DISPLAY_SCORE)
    candidates: dict[str, list[ReportItem]] = {}
    for section_key, _section_label, _category_keys in SECTION_DEFS:
        items = list(strict.get(section_key, []))
        if len(items) < THIN_SECTION_ITEMS:
            present = {item.story_key for item in items}
            items.extend(item for item in relaxed.get(section_key, []) if item.story_key not in present)
        candidates[section_key] = items
    selected: dict[str, list[ReportItem]] = {section_key: [] for section_key, _label, _keys in SECTION_DEFS}
    used: set[str] = set()

    def add_item(section_key: str, item: ReportItem) -> None:
        selected[section_key].append(item)
        used.add(item.story_key)

    for slot_index in range(BASE_SECTION_DISPLAY_ITEMS):
        for section_key, _section_label, _category_keys in SECTION_DEFS:
            section_items_for_slot = candidates.get(section_key, [])
            if len(section_items_for_slot) <= slot_index:
                continue
            if sum(len(items) for items in selected.values()) >= TARGET_DISPLAY_ITEMS:
                break
            item = section_items_for_slot[slot_index]
            if item.story_key not in used:
                add_item(section_key, item)

    if sum(len(items) for items in selected.values()) < TARGET_DISPLAY_ITEMS:
        remaining: list[tuple[str, ReportItem]] = [
            (section_key, item)
            for section_key, items in candidates.items()
            for item in items
            if item.story_key not in used
        ]
        remaining.sort(key=lambda pair: (pair[1].score, pair[1].total_volume_24h), reverse=True)
        for section_key, item in remaining:
            if sum(len(items) for items in selected.values()) >= TARGET_DISPLAY_ITEMS:
                break
            if len(selected[section_key]) >= MAX_SECTION_DISPLAY_ITEMS:
                continue
            add_item(section_key, item)

    return {section_key: items for section_key, items in selected.items() if items}


def strict_floor_item_count(sections: dict[str, list[ReportItem]]) -> int:
    """Displayed items that cleared the strict floor, not the thin-section relaxation."""
    total = 0
    for items in sections.values():
        for item in items:
            floor = (
                DISPLAY_SCORE
                if item.representative.translation_status == "ok"
                else DISPLAY_FALLBACK_SCORE
            )
            if item.score >= floor:
                total += 1
    return total


def interest_score(item: ReportItem, section_key: str) -> float:
    signal = item.representative
    probability = primary_probability(signal)
    move_1h = abs(signal.delta_1h or 0.0)
    move_24h = abs(signal.delta_24h or 0.0)
    move_score = min(20.0, max(move_1h * 160.0, move_24h * 120.0))

    tension_score = 0.0
    if probability is not None:
        center_distance = abs(probability - 0.5) * 2.0
        tension_score = max(0.0, 12.0 * (1.0 - center_distance))
        if (probability <= 0.03 or probability >= 0.97) and move_24h < 0.10:
            tension_score -= 8.0

    ttl_score = 2.0
    if signal.ttl_hours is not None:
        if 12 < signal.ttl_hours <= 72:
            ttl_score = 10.0
        elif 72 < signal.ttl_hours <= 168:
            ttl_score = 7.0
        elif 2 < signal.ttl_hours <= 12:
            ttl_score = 5.0
        elif signal.ttl_hours > 24 * 30:
            ttl_score = 1.0

    volume_score = min(16.0, math.log10(max(item.total_volume_24h, 1.0)) * 2.2)
    cluster_score = min(5.0, max(0, len(item.signals) - 1) * 1.5)
    section_bonus = SECTION_INTEREST_BONUS.get(section_key, 0.0)
    story_adjustment = next(
        (
            adjustment
            for suffix, adjustment in STORY_INTEREST_ADJUSTMENTS.items()
            if item.story_key.endswith(f":{suffix}")
        ),
        0.0,
    )
    base_score = item.score * 0.28
    return base_score + move_score + tension_score + ttl_score + volume_score + cluster_score + section_bonus + story_adjustment


def digest_event_title_cn(item: ReportItem, limit: int = 64) -> str:
    return clean_text(market_title_cn(item.representative), limit)


def digest_title_quality_ok(item: ReportItem, section_key: str | None = None) -> bool:
    signal = item.representative
    title = digest_event_title_cn(item)
    section_key = section_key or CATEGORY_TO_SECTION.get(signal.category.key, signal.category.key)
    if (
        signal.translation_status != "ok"
        or title_translation_status(title) != "ok"
        or primary_probability(signal) is None
    ):
        return False
    if any(item.story_key.endswith(f":{suffix}") for suffix in DIGEST_EXCLUDED_STORY_SUFFIXES):
        return False
    if "…" in title or not re.search(r"[\u3400-\u9fff]", title):
        return False
    if any(token in title for token in ("该结果", "事件：", "是否是否")):
        return False
    if section_key == "sports_culture" and (
        item.total_volume_24h < DIGEST_SPORTS_MIN_VOLUME_24H
        or not any(keyword in title for keyword in DIGEST_MAJOR_SPORTS_KEYWORDS)
    ):
        return False
    return True


def digest_items_by_interest(
    display_sections: dict[str, list[ReportItem]],
    limit: int = DIGEST_MAIN_ITEMS,
) -> list[tuple[str, ReportItem]]:
    candidates: list[tuple[str, ReportItem]] = [
        (section_key, item)
        for section_key, items in display_sections.items()
        for item in items
        if digest_title_quality_ok(item, section_key)
        and item.total_volume_24h >= DIGEST_MIN_VOLUME_24H
    ]
    candidates.sort(
        key=lambda pair: (
            interest_score(pair[1], pair[0]),
            pair[1].score,
            pair[1].total_volume_24h,
        ),
        reverse=True,
    )
    preferred = [pair for pair in candidates if pair[0] != "sports_culture"]
    sports = [pair for pair in candidates if pair[0] == "sports_culture"]
    selected: list[tuple[str, ReportItem]] = []
    used_sections: set[str] = set()
    for section_key, item in preferred + sports:
        if section_key in used_sections:
            continue
        selected.append((section_key, item))
        used_sections.add(section_key)
        if len(selected) >= limit:
            break
    return selected


def digest_primary_option(item: ReportItem) -> tuple[str, float | None]:
    signal = item.representative
    pairs = list(signal.outcome_prices)
    if not pairs and signal.probability is not None:
        pairs = [(signal.outcome, signal.probability)]
        if 0.0 <= signal.probability <= 1.0:
            pairs.append(("No", 1.0 - signal.probability))
    if not pairs:
        return "主要选项", signal.probability

    binary = {label.lower().strip() for label, _prob in pairs}
    if binary == {"yes", "no"}:
        yes_prob = next((prob for label, prob in pairs if label.lower().strip() == "yes"), None)
        return "是", yes_prob
    label, probability = max(pairs, key=lambda pair: pair[1])
    return translate_option_label(label, limit=16), probability


def digest_probability_plain(item: ReportItem) -> str:
    signal = item.representative
    pairs = list(signal.outcome_prices)
    if not pairs:
        label, probability = digest_primary_option(item)
        return f"{label} {format_prob_cn(probability)}"

    binary = {label.lower().strip() for label, _prob in pairs}
    if binary == {"yes", "no"}:
        yes_prob = next((prob for label, prob in pairs if label.lower().strip() == "yes"), None)
        no_prob = next((prob for label, prob in pairs if label.lower().strip() == "no"), None)
        return f"是 {format_prob_cn(yes_prob)}｜否 {format_prob_cn(no_prob)}"

    parts = [
        f"{translate_option_label(label, limit=14)} {format_prob_cn(prob)}"
        for label, prob in pairs[:2]
    ]
    return "｜".join(parts)


def format_digest_probability(item: ReportItem) -> str:
    plain = escape(digest_probability_plain(item))
    with_code = re.sub(r"(\d+(?:\.\d+)?%)", r"<code>\1</code>", plain)
    return "概率：" + with_code


def digest_hook_cn(item: ReportItem, section_key: str) -> str:
    signal = item.representative
    probability = primary_probability(signal)
    facts: list[str] = []
    if signal.delta_24h is not None:
        direction = "上升" if signal.delta_24h >= 0 else "下降"
        facts.append(f"市场概率 24 小时{direction}{abs(signal.delta_24h) * 100:.1f}点")
    if signal.ttl_hours is not None and 0 < signal.ttl_hours <= 168:
        days = max(1, math.ceil(signal.ttl_hours / 24))
        facts.append(f"{days}天内进入结算窗口")
    elif probability is not None and 0.35 <= probability <= 0.65:
        facts.append("当前仍处于明显分歧区")
    else:
        facts.append(f"24小时成交{format_money_cn(item.total_volume_24h)}")

    impact = {
        "geopolitics": "可能影响油价、黄金与风险偏好",
        "macro_finance": "可能影响利率、美元与风险资产定价",
        "crypto_web3": "反映加密市场风险偏好与资金方向",
        "tech_business": "对应 AI、科技巨头或商业主线预期",
        "politics_policy": "可能改变政策路径与市场叙事",
        "sports_culture": "仅因高关注赛事或显著异动进入头版",
    }.get(section_key, "代表新的市场分歧")
    return clean_text(f"{'；'.join(facts[:2])}。{impact}。", 88)


def digest_item_to_dict(section_key: str, item: ReportItem) -> dict[str, Any]:
    signal = item.representative
    primary_label, primary_probability_value = digest_primary_option(item)
    section_label = next(
        (label for key, label, _categories in SECTION_DEFS if key == section_key),
        section_key,
    )
    return {
        "section": section_key,
        "section_label": re.sub(r"^[^\w\u3400-\u9fff]+\s*", "", section_label),
        "story_key": item.story_key,
        "title": digest_event_title_cn(item),
        "group_title": item.title,
        "interest_score": round(interest_score(item, section_key), 2),
        "primary_label": primary_label,
        "primary_probability": primary_probability_value,
        "probability_line": digest_probability_plain(item),
        "why_care": digest_hook_cn(item, section_key),
        "representative_market_id": signal.market_id,
        "score": round(item.score, 2),
        "delta_24h": signal.delta_24h,
        "ttl_hours": signal.ttl_hours,
        "total_volume_24h": round(item.total_volume_24h, 2),
    }


def build_visual_source_payload(
    digest_items: list[tuple[str, ReportItem]],
    total_count: int,
    eligible_count: int,
    timestamp: str | None = None,
) -> dict[str, Any] | None:
    if not digest_items:
        return None
    section_key, item = digest_items[0]
    signal = item.representative
    primary_label, probability = digest_primary_option(item)
    if probability is None:
        return None
    section_label = next(
        (label for key, label, _categories in SECTION_DEFS if key == section_key),
        section_key,
    )
    timestamp = timestamp or f"{sgt_label()} SGT"
    return {
        "title": "预测市场雷达",
        "subtitle": "今日最值得先看",
        "section_label": re.sub(r"^[^\w\u3400-\u9fff]+\s*", "", section_label),
        "headline": digest_event_title_cn(item),
        "value_label": f"市场定价 · {primary_label}",
        "value": format_prob_cn(probability),
        "probability": probability,
        "delta_24h": format_delta_cn(signal.delta_24h),
        "volume": format_money_cn(item.total_volume_24h),
        "why_care": digest_hook_cn(item, section_key),
        "source": "Polymarket Gamma API",
        "timestamp": timestamp,
        "version": RADAR_VERSION,
        "accent": VISUAL_SECTION_ACCENTS.get(section_key, "#2563eb"),
        "candidate_count": total_count,
        "eligible_count": eligible_count,
    }


def build_visual_bundle(source_payload: dict[str, Any]) -> dict[str, Any]:
    evidence_roles = {
        "title": "category",
        "subtitle": "category",
        "section_label": "category",
        "headline": "category",
        "value_label": "category",
        "value": "scalar",
        "probability": "scalar",
        "delta_24h": "delta",
        "volume": "scalar",
        "why_care": "status",
        "source": "source",
        "timestamp": "time",
        "version": "source",
    }
    evidence = [
        {
            "label": key,
            "value": source_payload[key],
            "role": role,
            "source_path": f"$.{key}",
        }
        for key, role in evidence_roles.items()
    ]
    render_spec = {
        "version": "1.0",
        "kind": "hero",
        "title": source_payload["title"],
        "subtitle": source_payload["subtitle"],
        "theme": "light",
        "width": 1200,
        "accent": source_payload["accent"],
        "data": {
            "section_label": source_payload["section_label"],
            "headline": source_payload["headline"],
            "value_label": source_payload["value_label"],
            "value": source_payload["value"],
            "probability": source_payload["probability"],
            "delta_24h": source_payload["delta_24h"],
            "volume": source_payload["volume"],
            "why_care": source_payload["why_care"],
        },
        "meta": {
            "source": source_payload["source"],
            "timestamp": source_payload["timestamp"],
            "version": source_payload["version"],
        },
    }
    bindings = {
        "title": {"source_path": "$.title"},
        "subtitle": {"source_path": "$.subtitle"},
        "data.section_label": {"source_path": "$.section_label"},
        "data.headline": {"source_path": "$.headline"},
        "data.value_label": {"source_path": "$.value_label"},
        "data.value": {"source_path": "$.value"},
        "data.probability": {"source_path": "$.probability"},
        "data.delta_24h": {"source_path": "$.delta_24h"},
        "data.volume": {"source_path": "$.volume"},
        "data.why_care": {"source_path": "$.why_care"},
        "meta.source": {"source_path": "$.source"},
        "meta.timestamp": {"source_path": "$.timestamp"},
        "meta.version": {"source_path": "$.version"},
    }
    render_spec["source_bindings"] = bindings
    visual_spec = {
        "schema_version": "1.0",
        "primary_question": "今天预测市场最值得先看什么？",
        "headline": source_payload["headline"],
        "answer": source_payload["value"],
        "semantic_roles": sorted(set(evidence_roles.values())),
        "intents": ["state", "digest"],
        "evidence": evidence,
        "scores": {"text": 68, "image": 93, "video": 18},
        "selected_modality": "image",
        "fallback_chain": ["image", "text"],
        "selection_reason": "头条事件需要稳定层级和 0-100% 概率尺度，图片能在三秒内呈现答案。",
        "grammar": "hero-card",
        "feature_gate": {"rich_messages": False, "images": True, "videos": False},
        "warnings": [],
    }
    return {
        "source_payload": source_payload,
        "visual_spec": visual_spec,
        "render_spec": render_spec,
    }


def render_visual_card(
    digest_items: list[tuple[str, ReportItem]],
    total_count: int,
    eligible_count: int,
) -> dict[str, Any] | None:
    source_payload = build_visual_source_payload(digest_items, total_count, eligible_count)
    if source_payload is None:
        return None
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from polymarket_radar_visual import render_front_page

    bundle = build_visual_bundle(source_payload)
    image_path = OUTBOX_DIR / "latest-polymarket-daily.png"
    render_result = render_front_page(bundle, image_path)
    return {
        "path": image_path,
        "bundle": bundle,
        "source_payload": source_payload,
        **render_result,
    }


def write_visual_outbox(
    visual_artifact: dict[str, Any],
    caption: str,
    report_text: str,
    *,
    dry_run: bool,
    delivery: Any = None,
) -> dict[str, Any]:
    source = visual_artifact["source_payload"]
    image_path = Path(visual_artifact["path"])
    html_path = OUTBOX_DIR / "latest-polymarket-daily.html"
    detail_html_path = OUTBOX_DIR / "latest-polymarket-daily-detail.html"
    json_path = OUTBOX_DIR / "latest-polymarket-daily.json"
    write_text(html_path, caption)
    write_text(detail_html_path, report_text)
    outbox = {
        "ticker": "POLYMARKET",
        "job": "polymarket-daily",
        "level": "INFO",
        "headline": source["headline"],
        "image_path": str(image_path),
        "html_path": str(html_path),
        "detail_html_path": str(detail_html_path),
        "content_hash": content_hash(caption, report_text, canonical_json(visual_artifact["bundle"])),
        "caption_chars": visible_html_len(caption),
        "source": source["source"],
        "source_timestamp": source["timestamp"],
        "render_timestamp": source["timestamp"],
        "version": source["version"],
        "delivery": delivery if delivery is not None else ("dry_run" if dry_run else "pending"),
        "render_engine": visual_artifact["render_engine"],
        "font_warning": visual_artifact["font_warning"],
        "visual_spec": visual_artifact["bundle"]["visual_spec"],
        "render_spec": visual_artifact["bundle"]["render_spec"],
        "source_payload": source,
        "source_binding_validation": visual_artifact["validation"],
    }
    write_json(json_path, outbox)
    return {
        "image": str(image_path),
        "html": str(html_path),
        "json": str(json_path),
        "caption_chars": outbox["caption_chars"],
        "render_engine": outbox["render_engine"],
        "font_warning": outbox["font_warning"],
        "delivery": outbox["delivery"],
    }


def display_item_total(sections: dict[str, list[ReportItem]]) -> int:
    return sum(len(items) for items in sections.values())


def display_representative_signals(sections: dict[str, list[ReportItem]]) -> list[Signal]:
    return [item.representative for items in sections.values() for item in items]


def recent_display_story_count(
    signals: list[Signal],
    display_seen: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> int:
    stories = {story_key(signal) for signal in signals}
    return sum(1 for story in stories if display_story_recently_seen(story, display_seen, now=now))


def high_score_count(signals: list[Signal]) -> int:
    """Funnel diagnostic: how many eligible signals clear the display bar."""
    return sum(1 for signal in signals if signal.score >= DISPLAY_SCORE)


def render_digest_caption(
    display_sections: dict[str, list[ReportItem]],
    total_count: int,
    eligible_count: int,
    limit: int = DIGEST_MAIN_ITEMS,
) -> str:
    digest_items = digest_items_by_interest(display_sections, limit=limit)
    if not digest_items:
        return "⚠️ 仅供参考 #新闻雷达 #预测市场\n暂无符合头版质量门槛的新事件。"

    top_section, top_item = digest_items[0]
    top_signal = top_item.representative
    lines = [
        "⚠️ 仅供参考 #新闻雷达 #预测市场",
        f"📰 <b>今天先看：{escape(digest_event_title_cn(top_item))}</b>",
        "<blockquote>",
        f"{format_digest_probability(top_item)}",
        f"24小时 <code>{format_delta_cn(top_signal.delta_24h)}</code>｜"
        f"成交 <code>{format_money_cn(top_item.total_volume_24h)}</code>",
        escape(digest_hook_cn(top_item, top_section)),
        "</blockquote>",
    ]
    if len(digest_items) > 1:
        more_label = "另外一件" if len(digest_items) == 2 else "另外两件"
        lines.extend([f"<b>{more_label}</b>", "<blockquote expandable>"])
    for index, (section_key, item) in enumerate(digest_items[1:], start=2):
        signal = item.representative
        color_block = SECTION_COLOR_BLOCKS.get(section_key, "⬛")
        number_badge = NUMBER_BADGES[index - 1] if index <= len(NUMBER_BADGES) else f"{index}."
        lines.extend(
            [
                f"{number_badge} {color_block} <b>{escape(digest_event_title_cn(item))}</b>",
                f"{format_digest_probability(item)}｜24小时 <code>{format_delta_cn(signal.delta_24h)}</code>",
            ]
        )
    if len(digest_items) > 1:
        lines.append("</blockquote>")
    lines.append(f"<code>Polymarket · {escape(sgt_label())} SGT · {RADAR_VERSION}</code>")
    return "\n".join(lines)


def render_caption(
    signals: list[Signal],
    total_count: int,
    eligible_count: int | None = None,
    display_sections: dict[str, list[ReportItem]] | None = None,
) -> str:
    if not signals:
        return "⚠️ 仅供参考 #新闻雷达 #预测市场\n暂无强信号。"

    eligible_count = len(signals) if eligible_count is None else eligible_count
    display_sections = display_sections if display_sections is not None else report_items_by_section(signals)
    if not display_sections:
        lead = strongest_signal(signals)
        return "\n".join(
            [
                "⚠️ 仅供参考 #新闻雷达 #预测市场",
                f"📊 <b>预测市场雷达</b> · <code>{escape(sgt_label())} 新加坡时间</code>",
                f"候选 <code>{total_count}</code> 个｜合格 <code>{eligible_count}</code> 个｜最高热度 <code>{lead.score:.0f}</code>",
            ]
        )

    for limit in range(min(DIGEST_MAIN_ITEMS, display_item_total(display_sections)), 0, -1):
        caption = render_digest_caption(display_sections, total_count, eligible_count, limit=limit)
        if visible_html_len(caption) <= DIGEST_CAPTION_MAX_CHARS:
            return caption
    return "\n".join(
        [
            "⚠️ 仅供参考 #新闻雷达 #预测市场",
            "📊 <b>预测市场雷达</b> · 今日只看这 1 件",
            f"<code>{escape(sgt_label())} 新加坡时间</code>",
        ]
    )


def render_text_message(
    signals: list[Signal],
    total_count: int,
    eligible_count: int | None = None,
    display_sections: dict[str, list[ReportItem]] | None = None,
) -> str:
    eligible_count = len(signals) if eligible_count is None else eligible_count
    display_sections = display_sections if display_sections is not None else report_items_by_section(signals)
    display_count = display_item_total(display_sections)
    prefix = [
        "⚠️ 仅供参考 #新闻雷达 #预测市场",
        f"📚 <b>六大板块明细</b> · <code>{escape(sgt_label())} SGT</code>",
        f"扫描 <code>{total_count}</code>｜合格 <code>{eligible_count}</code>｜展示 <code>{display_count}</code>",
        "点击展开查看完整事件、概率、变化与成交。",
    ]
    footer = (
        "<i>概率来自预测市场交易价格，不代表事实或建议；"
        f"只读扫描，不做交易动作。{RADAR_VERSION}</i>"
    )
    body_chunks: list[str] = []
    truncated = False
    for section_key, section_label, category_keys in SECTION_DEFS:
        color_block = SECTION_COLOR_BLOCKS.get(section_key, "⬛")
        items = display_sections.get(section_key, [])
        if not items:
            continue
        block_lines = [f"{color_block} <b>{section_label}</b>"]
        for idx, item in enumerate(items, start=1):
            candidate_lines = block_lines + [report_line(item, idx, color_block)]
            candidate_body = "\n\n".join(body_chunks + ["\n".join(candidate_lines)])
            candidate_text = "\n\n".join(prefix + [f"<blockquote expandable>{candidate_body}</blockquote>", footer])
            if len(candidate_text) > 4000:
                truncated = True
                break
            block_lines = candidate_lines
        if len(block_lines) > 1:
            body_chunks.append("\n".join(block_lines))
        if truncated:
            break
    if truncated:
        body_chunks.append("其余低优先级条目已保留在本地运行记录中。")
    body = "\n\n".join(body_chunks)
    return "\n\n".join(prefix + [f"<blockquote expandable>{body}</blockquote>", footer])


def render_chart(
    signals: list[Signal],
    total_count: int,
    *,
    eligible_count: int | None = None,
    display_sections: dict[str, list[ReportItem]] | None = None,
) -> Path | None:
    """Compatibility wrapper for the V3 source-bound hero card."""
    if not signals:
        return None
    display_sections = display_sections if display_sections is not None else report_items_by_section(signals)
    digest_items = digest_items_by_interest(display_sections)
    artifact = render_visual_card(
        digest_items,
        total_count,
        len(signals) if eligible_count is None else eligible_count,
    )
    return Path(artifact["path"]) if artifact else None


def telegram_target(env: dict[str, str]) -> tuple[str, str, int]:
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("POLYMARKET_CHAT_ID", "").strip()
    thread = env.get("POLYMARKET_TOPIC_ID", "").strip()
    if not token:
        raise RuntimeError("Credential unavailable: TELEGRAM_BOT_TOKEN missing")
    if not chat_id:
        raise RuntimeError("Telegram target unavailable: POLYMARKET_CHAT_ID missing")
    if not thread:
        raise RuntimeError("Telegram target unavailable: POLYMARKET_TOPIC_ID missing")
    try:
        thread_id = int(thread)
    except ValueError as exc:
        raise RuntimeError("POLYMARKET_TOPIC_ID must be an integer") from exc
    return token, chat_id, thread_id


def monitor_target(env: dict[str, str]) -> tuple[str, str, int]:
    """Failure alerts go to the main topic unless POLYMARKET_ALERT_TOPIC_ID overrides it."""
    token, chat_id, thread_id = telegram_target(env)
    alert_thread = env.get("POLYMARKET_ALERT_TOPIC_ID", "").strip()
    if not alert_thread:
        return token, chat_id, thread_id
    try:
        return token, chat_id, int(alert_thread)
    except ValueError as exc:
        raise RuntimeError("POLYMARKET_ALERT_TOPIC_ID must be an integer") from exc


def alert_already_sent_today(kind: str, now: datetime) -> bool:
    """One alert per failure kind per day — enough to notice, not enough to spam."""
    today = now.astimezone(SGT).strftime("%Y-%m-%d")
    state: dict[str, Any] = {}
    if SOURCE_ALERT_STATE_PATH.exists():
        try:
            loaded = json.loads(SOURCE_ALERT_STATE_PATH.read_text(encoding="utf-8"))
            state = loaded if isinstance(loaded, dict) else {}
        except Exception:  # noqa: BLE001
            state = {}
    entry = state.get(kind)
    already = isinstance(entry, dict) and entry.get("date") == today
    if already:
        entry["count"] = int(entry.get("count") or 1) + 1
    else:
        state[kind] = {"date": today, "count": 1, "last": now.isoformat(timespec="seconds")}
    write_json(SOURCE_ALERT_STATE_PATH, state)
    return already


def notify_monitor(kind: str, text: str, dry_run: bool) -> dict[str, Any]:
    """Best-effort alert to the Monitor topic. Never raises into the caller."""
    now = now_utc()
    if dry_run:
        print(f"[MONITOR_ALERT_DRY_RUN] {kind}\n{text}")
        return {"kind": kind, "status": "dry_run"}
    if alert_already_sent_today(kind, now):
        return {"kind": kind, "status": "deduped"}
    try:
        token, chat_id, thread_id = monitor_target(load_env())
        result = send_message(token, chat_id, thread_id, text)
        return {
            "kind": kind,
            "status": "sent",
            "message_id": result.get("result", {}).get("message_id"),
            "message_thread_id": thread_id,
        }
    except Exception as exc:  # noqa: BLE001
        # The radar is already failing; a failing alert must not mask the
        # original error by replacing it with its own traceback.
        print(f"[MONITOR_ALERT_FAILED] {kind} {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return {"kind": kind, "status": "alert_failed", "error_type": type(exc).__name__}


def notify_source_failure(source_health: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    reasons = ", ".join(source_health.get("reasons") or []) or "unknown"
    empty = ", ".join(source_health.get("empty_sources") or []) or "-"
    lines = [
        "🔴 <b>Polymarket 雷达 · 数据源失效</b>",
        f"时间：<code>{sgt_label()}</code> SGT",
        f"原因：<code>{escape(reasons[:300])}</code>",
        f"空源：<code>{escape(empty[:200])}</code>",
        f"抓取失败次数：<code>{source_health.get('fetch_failure_count', 0)}</code>",
        "",
        "本轮已跳过推送并以退出码 1 结束。",
    ]
    return notify_monitor("source_failure", "\n".join(lines), dry_run)


def notify_send_failure(exc: BaseException, dry_run: bool) -> dict[str, Any]:
    lines = [
        "🔴 <b>Polymarket 雷达 · Telegram 推送失败</b>",
        f"时间：<code>{sgt_label()}</code> SGT",
        f"异常：<code>{escape(type(exc).__name__)}</code>",
        f"信息：<code>{escape(str(exc)[:300])}</code>",
        "",
        "本轮不写 display 冷却，下一轮会重试同一批故事。",
    ]
    return notify_monitor("send_failed", "\n".join(lines), dry_run)


def rotate_history(path: Path, max_bytes: int = HISTORY_MAX_BYTES, keep: int = HISTORY_KEEP_RECORDS) -> bool:
    """Keep the newest records once the append-only log crosses the size cap."""
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return False
    if len(lines) <= keep:
        return False
    archive = path.with_suffix(path.suffix + ".1")
    try:
        path.replace(archive)
        path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    return True


def is_transport_error(exc: BaseException) -> bool:
    """True only for failures curl could plausibly succeed at retrying.

    A rejected payload (bad HTML, caption too long, wrong thread id) returns
    the same 4xx no matter which client sends it, so retrying via curl only
    hides the real error behind a second identical one.
    """
    if requests is not None:
        transport = getattr(requests, "exceptions", None)
        if transport is not None and isinstance(
            exc,
            (
                getattr(transport, "ConnectionError", ()),
                getattr(transport, "Timeout", ()),
                getattr(transport, "SSLError", ()),
                getattr(transport, "ProxyError", ()),
            ),
        ):
            return True
        http_error = getattr(getattr(requests, "exceptions", None), "HTTPError", None)
        if http_error is not None and isinstance(exc, http_error):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return status is not None and status >= 500
    return isinstance(exc, (OSError, TimeoutError))


def redact_bot_token(text: str) -> str:
    """Strip the bot token from error text before it reaches logs.

    requests exceptions embed the full request URL, and the Telegram URL
    carries the token in its path — re-raising that raw would leak the
    credential into launchd err.log.
    """
    return re.sub(r"/bot[^/\s'\"]+", "/bot<redacted>", text)


def send_message(token: str, chat_id: str, thread_id: int, text: str) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if requests is not None:
        try:
            resp = requests.post(url, json=payload, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram sendMessage failed: {data}")
            return data
        except Exception as exc:  # noqa: BLE001
            if not is_transport_error(exc):
                error_text = redact_bot_token(str(exc).replace(token, "<redacted>"))
                raise RuntimeError(
                    f"Telegram sendMessage request failed: {type(exc).__name__}: {error_text}"
                ) from exc
    return send_message_via_curl(url, chat_id, thread_id, text)


def send_message_via_curl(url: str, chat_id: str, thread_id: int, text: str) -> dict[str, Any]:
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        "20",
        "-X",
        "POST",
        url,
        "--data-urlencode",
        f"chat_id={chat_id}",
        "--data-urlencode",
        f"message_thread_id={thread_id}",
        "--data-urlencode",
        "parse_mode=HTML",
        "--data-urlencode",
        "disable_web_page_preview=true",
        "--data-urlencode",
        f"text={text}",
    ]
    resp = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if resp.returncode != 0:
        raise RuntimeError(
            "curl sendMessage failed: "
            + redact_bot_token(resp.stderr.strip() or resp.stdout.strip())
        )
    data = json.loads(resp.stdout)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {resp.stdout}")
    return data


def send_photo(token: str, chat_id: str, thread_id: int, image_path: Path, caption: str) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    if requests is not None:
        try:
            with image_path.open("rb") as handle:
                resp = requests.post(
                    url,
                    data={
                        "chat_id": chat_id,
                        "message_thread_id": str(thread_id),
                        "parse_mode": "HTML",
                        "caption": caption,
                    },
                    files={"photo": (image_path.name, handle, "image/png")},
                    timeout=30,
                )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram sendPhoto failed: {data}")
            return data
        except Exception as exc:  # noqa: BLE001
            if not is_transport_error(exc):
                error_text = redact_bot_token(str(exc).replace(token, "<redacted>"))
                raise RuntimeError(
                    f"Telegram sendPhoto request failed: {type(exc).__name__}: {error_text}"
                ) from exc
    return send_photo_via_curl(url, chat_id, thread_id, image_path, caption)


def send_photo_via_curl(url: str, chat_id: str, thread_id: int, image_path: Path, caption: str) -> dict[str, Any]:
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        "30",
        "-X",
        "POST",
        url,
        # --form-string, not -F: a caption beginning with "@" or "<" would
        # otherwise be read by curl as a file path to upload.
        "--form-string",
        f"chat_id={chat_id}",
        "--form-string",
        f"message_thread_id={thread_id}",
        "--form-string",
        "parse_mode=HTML",
        "--form-string",
        f"caption={caption}",
        "-F",
        f"photo=@{image_path}",
    ]
    resp = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if resp.returncode != 0:
        raise RuntimeError(
            "curl sendPhoto failed: "
            + redact_bot_token(resp.stderr.strip() or resp.stdout.strip())
        )
    data = json.loads(resp.stdout)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {resp.stdout}")
    return data


def deliver_digest(
    token: str,
    chat_id: str,
    thread_id: int,
    caption: str,
    chart_path: Path | None,
    visual_error: dict[str, str] | None = None,
) -> dict[str, Any]:
    if chart_path:
        result = send_photo(token, chat_id, thread_id, chart_path, caption)
        return {
            "method": "sendPhoto",
            "photo_message_id": result.get("result", {}).get("message_id"),
        }
    result = send_message(token, chat_id, thread_id, caption)
    return {
        "method": "sendMessage",
        "summary_message_id": result.get("result", {}).get("message_id"),
        "visual_fallback": visual_error,
    }


def run(args: argparse.Namespace) -> int:
    with single_instance_lock(RUN_LOCK_PATH):
        return _run_locked(args)


def _run_locked(args: argparse.Namespace) -> int:
    run_now = now_utc()
    probe_snapshot = load_probe_snapshot()
    probe_markets = probe_snapshot.get("markets")
    if not isinstance(probe_markets, dict):
        probe_markets = {}
    collection = collect_signal_result(args.limit)
    signals = collection.signals
    source_health = collection.source_health
    translation_stats = apply_llm_translations(signals, enabled=not args.no_llm, now=run_now)
    display_seen = {} if args.force else load_display_seen()
    display_sections = report_items_by_section(signals, display_seen=display_seen, now=run_now)
    display_count = display_item_total(display_sections)
    display_signals = display_representative_signals(display_sections)
    digest_items = digest_items_by_interest(display_sections)
    caption_signals = display_signals[:MAX_CAPTION_SIGNALS] or signals[:MAX_CAPTION_SIGNALS]
    high_count = high_score_count(signals)
    recent_display_suppressed_count = 0 if args.force else recent_display_story_count(signals, display_seen, now=run_now)
    eligible_by_section = Counter(CATEGORY_TO_SECTION.get(signal.category.key, signal.category.key) for signal in signals)
    translation_status_counts = Counter(signal.translation_status for signal in signals)
    translation_engine_counts = Counter(signal.translation_engine for signal in signals)
    source_status_counts = Counter(signal.source_status for signal in signals)
    rejected_by_reason = Counter(str(item.get("reject_reason") or "unknown") for item in collection.rejected)
    latest_payload = {
        "timestamp": iso_now(),
        "radar_version": RADAR_VERSION,
        "dry_run": args.dry_run,
        "candidate_count": collection.raw_candidate_count,
        "eligible_count": len(signals),
        "pushed_count": len(digest_items),
        "high_score_count": high_count,
        "display_target": TARGET_DISPLAY_ITEMS,
        "display_count": display_count,
        "display_strict_floor_count": strict_floor_item_count(display_sections),
        "display_by_section": {section_key: len(items) for section_key, items in display_sections.items()},
        "display_recent_suppressed_count": recent_display_suppressed_count,
        "eligible_by_section": dict(eligible_by_section),
        "translation_status_counts": dict(translation_status_counts),
        "translation_engine_counts": dict(translation_engine_counts),
        "translation_llm": translation_stats,
        "source_status_counts": dict(source_status_counts),
        "rejected_by_reason": dict(rejected_by_reason),
        "source_counts": collection.source_counts,
        "source_timings": {source: dict(timing) for source, timing in FETCH_TIMINGS.items()},
        "source_health": source_health,
        "top_candidates": [signal_to_dict(signal) for signal in signals[:12]],
        "display_items": [
            report_item_to_dict(item, section_key)
            for section_key, items in display_sections.items()
            for item in items
        ],
        "digest_items": [
            digest_item_to_dict(section_key, item)
            for section_key, item in digest_items
        ],
        "rejected_candidates": collection.rejected[:80],
    }
    delta_matched = 0
    for display_item in latest_payload["display_items"]:
        for display_market in display_item["display_markets"]:
            delta = probability_delta_since_last_run(
                display_market["market_id"], display_market["probability"], probe_markets
            )
            display_market["probability_delta_run"] = delta
            if delta is not None:
                delta_matched += 1
    latest_payload["probe_snapshot"] = {
        "previous_ts": probe_snapshot.get("ts"),
        "previous_markets": len(probe_markets),
        "display_markets_matched": delta_matched,
    }
    write_json(LATEST_PATH, latest_payload)
    rotate_history(HISTORY_PATH)

    if source_health.get("status") == "failed":
        # A blind run and a quiet market both produce zero pushes. Only this
        # branch knows the difference, so it must be the loud one.
        append_jsonl(
            HISTORY_PATH,
            {
                "timestamp": latest_payload["timestamp"],
                "status": "source_failure",
                "candidate_count": collection.raw_candidate_count,
                "eligible_count": len(signals),
                "source_health": source_health,
            },
        )
        notify_source_failure(source_health, dry_run=args.dry_run)
        print(
            "POLYMARKET_SOURCE_FAILURE "
            f"candidates={collection.raw_candidate_count} "
            f"reasons={','.join(source_health.get('reasons') or []) or 'unknown'}"
        )
        return 1

    # Only a healthy run may advance the probability baseline: a blind run has
    # an empty eligible set and would wipe the previous snapshot.
    write_probe_snapshot(signals, run_now)

    if not display_count:
        append_jsonl(
            HISTORY_PATH,
            {
                "timestamp": latest_payload["timestamp"],
                "status": "no_fresh_display",
                "candidate_count": collection.raw_candidate_count,
                "eligible_count": len(signals),
                "high_score_count": high_count,
                "display_recent_suppressed_count": recent_display_suppressed_count,
                "source_health_status": source_health.get("status"),
                "top_score": round(signals[0].score, 2) if signals else None,
            },
        )
        print(
            f"NO_POLYMARKET_FRESH_DISPLAY candidates={collection.raw_candidate_count} eligible={len(signals)} recent_suppressed={recent_display_suppressed_count} top_score={signals[0].score:.1f}"
            if signals
            else f"NO_POLYMARKET_FRESH_DISPLAY candidates={collection.raw_candidate_count} eligible=0"
        )
        return 0

    if not digest_items:
        append_jsonl(
            HISTORY_PATH,
            {
                "timestamp": latest_payload["timestamp"],
                "status": "no_quality_digest",
                "candidate_count": collection.raw_candidate_count,
                "eligible_count": len(signals),
                "display_count": display_count,
                "display_recent_suppressed_count": recent_display_suppressed_count,
                "source_health_status": source_health.get("status"),
            },
        )
        print(
            "NO_POLYMARKET_QUALITY_DIGEST "
            f"candidates={collection.raw_candidate_count} eligible={len(signals)} "
            f"display={display_count}"
        )
        return 0

    caption = render_caption(
        caption_signals,
        collection.raw_candidate_count,
        eligible_count=len(signals),
        display_sections=display_sections,
    )
    report_text = render_text_message(
        signals,
        collection.raw_candidate_count,
        eligible_count=len(signals),
        display_sections=display_sections,
    )
    visual_artifact: dict[str, Any] | None = None
    visual_error: dict[str, str] | None = None
    if not args.no_image:
        try:
            visual_artifact = render_visual_card(
                digest_items,
                collection.raw_candidate_count,
                len(signals),
            )
        except Exception as exc:  # noqa: BLE001
            visual_error = {"type": type(exc).__name__, "message": str(exc)}
    chart_path = Path(visual_artifact["path"]) if visual_artifact else None
    outbox_evidence = (
        write_visual_outbox(
            visual_artifact,
            caption,
            report_text,
            dry_run=args.dry_run,
        )
        if visual_artifact
        else None
    )
    latest_payload["visual"] = {
        "selected_modality": "image" if visual_artifact else "text",
        "chart_path": str(chart_path) if chart_path else None,
        "outbox": outbox_evidence,
        "error": visual_error,
    }
    write_json(LATEST_PATH, latest_payload)

    if args.dry_run:
        append_jsonl(
            HISTORY_PATH,
            {
                "timestamp": latest_payload["timestamp"],
                "status": "dry_run",
                "candidate_count": collection.raw_candidate_count,
                "eligible_count": len(signals),
                "pushed_count": len(digest_items),
                "high_score_count": high_count,
                "display_count": display_count,
                "display_by_section": {section_key: len(items) for section_key, items in display_sections.items()},
                "display_recent_suppressed_count": recent_display_suppressed_count,
                "source_health_status": source_health.get("status"),
                "digest_items": [
                    digest_item_to_dict(section_key, item)
                    for section_key, item in digest_items
                ],
                "chart_path": str(chart_path) if chart_path else None,
                "outbox": outbox_evidence,
                "visual_error": visual_error,
            },
        )
        print("[DRY_RUN]")
        print("[CAPTION]")
        print(caption)
        print("[REPORT]")
        print(report_text)
        if chart_path:
            print(f"[CHART] {chart_path}")
        if outbox_evidence:
            print(f"[OUTBOX] {json.dumps(outbox_evidence, ensure_ascii=False)}")
        if visual_error:
            print(f"[VISUAL_FALLBACK] {json.dumps(visual_error, ensure_ascii=False)}")
        if args.explain:
            print("[EXPLAIN_SOURCE_HEALTH]")
            print(json.dumps(source_health, ensure_ascii=False))
            print("[EXPLAIN_DISPLAYED]")
            for section_key, item in digest_items:
                signal = item.representative
                print(
                    json.dumps(
                        {
                            "section": section_key,
                            "market_title": signal.market_title,
                            "market_title_cn": signal.market_title_cn,
                            "story_key": story_key(signal),
                            "score": round(signal.score, 2),
                            "reason": signal.reason,
                            "deadline": signal.effective_deadline_sgt,
                            "ttl_hours": signal.ttl_hours,
                            "source_status": signal.source_status,
                            "category_reason": signal.category_reason,
                            "translation_status": signal.translation_status,
                            "translation_engine": signal.translation_engine,
                            "score_breakdown": signal.score_breakdown,
                        },
                        ensure_ascii=False,
                    )
                )
            print("[EXPLAIN_REJECTED]")
            for item in collection.rejected[:30]:
                print(json.dumps(item, ensure_ascii=False))
        return 0

    env = load_env()
    try:
        token, chat_id, thread_id = telegram_target(env)
    except RuntimeError as exc:
        # Missing credentials used to die before the send try/except below,
        # leaving no history record for the freshness sentinel to notice.
        append_jsonl(
            HISTORY_PATH,
            {
                "timestamp": latest_payload["timestamp"],
                "status": "config_error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:400],
                "candidate_count": collection.raw_candidate_count,
                "eligible_count": len(signals),
                "pushed_count": 0,
            },
        )
        raise
    try:
        delivery = deliver_digest(
            token,
            chat_id,
            thread_id,
            caption,
            chart_path,
            visual_error,
        )
    except Exception as exc:  # noqa: BLE001
        append_jsonl(
            HISTORY_PATH,
            {
                "timestamp": latest_payload["timestamp"],
                "status": "send_failed",
                "error_type": type(exc).__name__,
                "error": str(exc)[:400],
                "candidate_count": collection.raw_candidate_count,
                "eligible_count": len(signals),
                "pushed_count": 0,
                "digest_items": [
                    digest_item_to_dict(section_key, item)
                    for section_key, item in digest_items
                ],
            },
        )
        notify_send_failure(exc, dry_run=False)
        raise

    mark_digest_sent(digest_items)
    if visual_artifact:
        outbox_evidence = write_visual_outbox(
            visual_artifact,
            caption,
            report_text,
            dry_run=False,
            delivery=delivery,
        )
    latest_payload["delivery"] = delivery
    latest_payload["visual"]["outbox"] = outbox_evidence
    write_json(LATEST_PATH, latest_payload)
    append_jsonl(
        HISTORY_PATH,
        {
            "timestamp": latest_payload["timestamp"],
            "status": "sent",
            "candidate_count": collection.raw_candidate_count,
            "eligible_count": len(signals),
            "pushed_count": len(digest_items),
            "high_score_count": high_count,
            "display_count": display_count,
            "display_by_section": {section_key: len(items) for section_key, items in display_sections.items()},
            "display_recent_suppressed_count": recent_display_suppressed_count,
            "source_health_status": source_health.get("status"),
            "delivery": delivery,
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "digest_items": [
                digest_item_to_dict(section_key, item)
                for section_key, item in digest_items
            ],
            "outbox": outbox_evidence,
            "visual_error": visual_error,
            "display_items": [
                report_item_to_dict(item, section_key)
                for section_key, items in display_sections.items()
                for item in items
            ],
        },
    )
    print(
        json.dumps(
            {
                "status": "sent",
                "candidate_count": collection.raw_candidate_count,
                "eligible_count": len(signals),
                "pushed_count": len(digest_items),
                "high_score_count": high_count,
                "display_count": display_count,
                "display_recent_suppressed_count": recent_display_suppressed_count,
                "source_health_status": source_health.get("status"),
                "delivery": delivery,
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "history": str(HISTORY_PATH),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Polymarket sentiment radar for Telegram.")
    parser.add_argument("--limit", type=int, default=120, help="Per-query Gamma events limit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram or mark cooldown.")
    parser.add_argument("--force", action="store_true", help="Ignore the display cooldown for this run.")
    parser.add_argument("--no-image", action="store_true", help="Send text only.")
    parser.add_argument("--no-llm", action="store_true", help="Skip the LLM translation pass; rule translation only.")
    parser.add_argument("--explain", action="store_true", help="Print display/rejected diagnostics during dry-run.")
    parser.add_argument("--selftest", action="store_true", help="Run offline regression checks (no network, no Telegram) and exit.")
    return parser


def selftest() -> int:
    """Offline regression checks: pure functions only — no Gamma, no Telegram.

    Lets a deploy box prove the translation/scoring core is intact with
    `--selftest`; the pytest suite in tests/ remains the full authority.
    """
    import tempfile

    failures: list[str] = []
    total = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal total
        total += 1
        print(("PASS " if ok else "FAIL ") + name + (f" | {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    text = localize_market_text("Will Reform UK win the Clacton by-election?")
    check("by-election survives", "截至" not in text and "by-election" in text, text)
    text = localize_market_text("Ninjas in Pyjamas vs Team Spirit (BO3)")
    check("'in Pyjamas' survives", "在" not in text and "in Pyjamas" in text, text)
    text = localize_market_text("Dota 2: Team Falcons vs LGD Gaming (BO3) - The International Group Stage")
    check("esports dash survives", "至" not in text and "-" in text, text)
    text = localize_market_text("Elon Musk # of tweets July 18 - 25?")
    check("date range keeps 至", "至25" in text.replace(" ", ""), text)
    text = localize_market_text("Will the US strike Iran by August 30?")
    check("uppercase US translated", "美国" in text and "伊朗" in text, text)
    text = localize_market_text("Will they let us join the group?")
    check("pronoun 'us' untouched", "美国" not in text, text)
    text = localize_market_text("F-16 delivery to Ukraine announced?")
    check("F-16 survives", "F-16" in text, text)

    deadline, _ = infer_question_deadline(
        "Will X happen by August 20?", reference=datetime(2026, 8, 15, tzinfo=timezone.utc)
    )
    check("by-<date> exclusive deadline", deadline == datetime(2026, 8, 21, tzinfo=timezone.utc), str(deadline))
    deadline, _ = infer_question_deadline(
        "Will X happen by January 5?", reference=datetime(2026, 12, 20, tzinfo=timezone.utc)
    )
    check("deadline year rollover", deadline is not None and deadline.year == 2027, str(deadline))
    deadline, _ = infer_question_deadline("Will X happen by February 30, 2026?")
    check("malformed date tolerated", deadline is None, str(deadline))
    check("month map matches calendar", [MONTH_NUMBER_EN[month] for month in MONTHS_CN] == list(range(1, 13)))

    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "nested" / "atomic.json"
        write_json(target, {"a": 1})
        write_json(target, {"a": 2})
        leftovers = list(target.parent.glob("*.tmp-*"))
        check("write_json atomic + clean", json.loads(target.read_text()) == {"a": 2} and not leftovers, str(leftovers))

    section_keys = [section_key for section_key, _label, _categories in SECTION_DEFS]
    check("seven sections defined", len(section_keys) == 7, ",".join(section_keys))
    for table_name, table in (
        ("color block", SECTION_COLOR_BLOCKS),
        ("interest bonus", SECTION_INTEREST_BONUS),
        ("visual accent", VISUAL_SECTION_ACCENTS),
        ("discovery queries", DISCOVERY_QUERIES_BY_SECTION),
    ):
        check(f"every section has a {table_name}", all(key in table for key in section_keys))
    check(
        "society categories mapped",
        CATEGORY_TO_SECTION.get("society_weather_health_science") == "society_science"
        and CATEGORY_TO_SECTION.get("other") == "society_science",
    )

    tail_market = {
        "volume24hr": 250_000,
        "liquidityNum": 60_000,
        "oneDayPriceChange": 0.05,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.97", "0.03"]',
    }
    _, _, tail_breakdown = score_signal({}, tail_market, OTHER_CATEGORY, 24.0, "ok", "confirmed")
    _, _, mid_breakdown = score_signal(
        {}, dict(tail_market, outcomePrices='["0.50", "0.50"]'), OTHER_CATEGORY, 24.0, "ok", "confirmed"
    )
    check(
        "tail-probability move damped",
        tail_breakdown.get("move_tail_damped") == 1.0
        and mid_breakdown.get("move_tail_damped") == 0.0
        and tail_breakdown["move"] < mid_breakdown["move"],
        f"tail={tail_breakdown['move']} mid={mid_breakdown['move']}",
    )

    check(
        "probe delta math",
        probability_delta_since_last_run("m1", 0.62, {"m1": {"p": 0.5}}) == 0.12
        and probability_delta_since_last_run("m2", 0.62, {}) is None,
    )

    print(f"SELFTEST {'FAIL' if failures else 'OK'} passed={total - len(failures)}/{total}")
    return 1 if failures else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
