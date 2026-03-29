"""
syria_news.py
Fetches Syria-focused news from English-language RSS feeds, categorizes
stories, and writes them to docs/syria_news.json — capped at 20 per
category, max age 7 days, oldest entries replaced first.
No external APIs are used. All sources publish in English.
"""

import json
import os
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser
import feedparser

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_DIR = "docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "syria_news.json")
MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7
CATEGORIES = ["Diplomacy", "Military", "Energy", "Economy", "Local Events"]

# RSS feeds — all free, English-language, Syria-focused, no APIs
FEEDS = [
    # Syrian Observer — daily English-language Syria news, confirmed active March 2026
    {"source": "Syrian Observer", "url": "https://syrianobserver.com/feed/"},
    # Syria Direct — fiercely independent English-language Syrian journalism
    {"source": "Syria Direct", "url": "https://syriadirect.org/feed/"},
    {"source": "Syria Direct", "url": "https://syriadirect.org/category/news/feed/"},
    # Al Jazeera — dedicated Syria section (English)
    {"source": "Al Jazeera", "url": "https://www.aljazeera.com/where/syria/feed"},
    {"source": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    # The Guardian — Syria section
    {"source": "The Guardian", "url": "https://www.theguardian.com/world/syria/rss"},
    # SANA English — Syrian Arab News Agency official English service
    # Provides government/transitional authority perspective
    {"source": "SANA", "url": "https://sana.sy/en/?feed=rss2"},
    # BBC News Middle East — strong Syria post-Assad coverage
    {"source": "BBC News", "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
]

# Syria-specific anchor terms for filtering broad feeds
SYRIA_ANCHORS = [
    "syria", "syrian", "damascus", "aleppo", "homs", "idlib",
    "deir ez-zor", "raqqa", "daraa", "latakia", "hama",
    "hayat tahrir al-sham", "hts", "jolani", "al-sharaa",
    "sdf", "ypg", "kurdish forces", "isis in syria",
    "assad", "post-assad", "transitional government syria",
]

# Feeds that cover multiple countries and need Syria filtering
REQUIRE_ANCHOR = {"Al Jazeera", "BBC News"}

# ---------------------------------------------------------------------------
# Category keyword mapping (Syria-contextualised)
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Diplomacy": [
        "diplomacy", "diplomatic", "foreign policy", "embassy", "ambassador",
        "treaty", "bilateral", "multilateral", "united nations", "un security",
        "foreign minister", "foreign affairs", "summit", "sanctions",
        "international relations", "geopolitical", "arab league",
        "trade deal", "accord", "alliance", "envoy", "consul",
        "peace talks", "ceasefire", "negotiations", "reconstruction aid",
        "al-sharaa", "jolani", "transitional government",
        "syrian government", "damascus government",
        "syria and turkey", "syria and israel", "syria and us",
        "syria and russia", "syria and iran", "syria and jordan",
        "syria and saudi", "syria and eu", "normalization",
        "lifting sanctions", "un resolution", "un envoy",
        "arab summit", "arab states", "arab normalization",
        "recognition", "diplomatic relations restored",
    ],
    "Military": [
        "military", "army", "navy", "air force", "defence", "defense",
        "troops", "soldier", "weapons", "missile", "armed forces",
        "war", "combat", "conflict", "bomb", "explosion", "airstrike",
        "shelling", "gunfire", "hts", "hayat tahrir al-sham",
        "sdf", "ypg", "pkk", "islamic state", "isis", "daesh",
        "turkish forces", "turkish military", "israeli airstrike",
        "us forces", "kurdish forces", "rebel", "faction",
        "killed", "casualties", "wounded", "clashes",
        "offensive", "operation", "raid", "checkpoint",
        "weapons cache", "arms", "chemical weapons",
        "idlib", "deir ez-zor", "euphrates", "military base",
        "jolani military", "hts military", "new syrian army",
    ],
    "Energy": [
        "energy", "oil", "gas", "petroleum", "oil fields",
        "syrian oil", "deir ez-zor oil", "al-omar field",
        "renewable", "solar", "wind", "electricity", "power grid",
        "blackout", "power cut", "fuel", "diesel", "fuel shortage",
        "climate", "emissions", "environment",
        "energy crisis", "generator", "power plant",
        "reconstruction energy", "energy infrastructure",
        "oil smuggling", "oil revenue", "natural gas",
        "euphrates dam", "tabqa dam", "water energy",
    ],
    "Economy": [
        "economy", "economic", "gdp", "inflation", "unemployment",
        "jobs", "budget", "finance", "tax", "investment", "business",
        "trade", "syrian pound", "syp", "exchange rate",
        "imf", "world bank", "donor", "aid", "reconstruction",
        "development", "sanctions", "caesar act", "sanctions relief",
        "debt", "poverty", "remittance", "banking",
        "exports", "imports", "agriculture", "wheat",
        "food security", "bread", "subsidy", "market",
        "reconstruction fund", "economic recovery",
        "business environment", "foreign investment",
        "free zones", "trade routes",
    ],
    "Local Events": [
        "local", "province", "governorate", "community",
        "hospital", "school", "university", "crime", "court",
        "flood", "earthquake", "fire", "transport", "protest",
        "displacement", "refugee", "idp", "return",
        "damascus", "aleppo", "homs", "idlib", "latakia",
        "hama", "daraa", "suweida", "hasakah", "qamishli",
        "deir ez-zor", "raqqa", "kobane", "afrin",
        "refugee return", "displaced return", "internally displaced",
        "humanitarian", "civilian", "ngo", "aid delivery",
        "reconstruction", "rubble", "destroyed homes",
        "civil society", "women rights", "minority rights",
        "druze", "alawite", "christian minority", "kurd",
        "election", "transitional justice", "reconciliation",
        "detention", "prisoners", "missing", "disappeared",
        "cholera", "disease", "health crisis", "malnutrition",
    ],
}


def is_syria_story(title: str, description: str) -> bool:
    """Check whether a story is meaningfully about Syria."""
    text = (title + " " + (description or "")).lower()
    return any(anchor in text for anchor in SYRIA_ANCHORS)


def classify(title: str, description: str):
    """Return the best-matching category for a story, or None if no match."""
    text = (title + " " + (description or "")).lower()
    scores = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw) + r'\b', text):
                scores[cat] += 1
    best_cat = max(scores, key=scores.get)
    return best_cat if scores[best_cat] > 0 else None


def strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_date(entry):
    """Parse a feed entry's published date into a UTC-aware datetime."""
    raw = entry.get("published") or entry.get("updated") or entry.get("created")
    if not raw:
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if struct:
            return datetime(*struct[:6], tzinfo=timezone.utc)
        return None
    try:
        dt = dateparser.parse(raw)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc) if dt else None
    except Exception:
        return None


def fetch_feed(feed_cfg: dict) -> list:
    """Fetch a single RSS feed and return a list of story dicts."""
    source = feed_cfg["source"]
    url = feed_cfg["url"]
    stories = []
    try:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            log.warning("Bozo feed (%s): %s", source, url)
            return stories
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        for entry in parsed.entries:
            pub_date = parse_date(entry)
            if pub_date and pub_date < cutoff:
                continue
            title = strip_html(entry.get("title", "")).strip()
            desc = strip_html(entry.get("summary", "")).strip()
            if not title:
                continue
            # Filter multi-country feeds by Syria anchor terms
            if source in REQUIRE_ANCHOR and not is_syria_story(title, desc):
                continue
            category = classify(title, desc)
            if not category:
                continue
            story = {
                "title": title,
                "source": source,
                "url": entry.get("link", ""),
                "published_date": pub_date.isoformat() if pub_date else None,
                "category": category,
            }
            stories.append(story)
    except Exception as exc:
        log.error("Failed to fetch %s (%s): %s", source, url, exc)
    return stories


def load_existing() -> dict:
    """Load the current JSON file, grouped by category."""
    if not os.path.exists(OUTPUT_FILE):
        return {cat: [] for cat in CATEGORIES}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {cat: [] for cat in CATEGORIES}

    grouped = {cat: [] for cat in CATEGORIES}
    stories = data.get("stories", data) if isinstance(data, dict) else data
    if isinstance(stories, list):
        for story in stories:
            cat = story.get("category")
            if cat in grouped:
                grouped[cat].append(story)
    return grouped


def merge(existing: dict, fresh: list) -> dict:
    """
    Merge fresh stories into the existing pool.
    - De-duplicate by URL.
    - Discard stories older than MAX_AGE_DAYS.
    - Replace oldest entries first when over MAX_PER_CATEGORY.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    existing_urls = set()
    for stories in existing.values():
        for s in stories:
            if s.get("url"):
                existing_urls.add(s["url"])

    for story in fresh:
        cat = story.get("category")
        if cat not in existing:
            continue
        if story["url"] in existing_urls:
            continue
        existing[cat].append(story)
        existing_urls.add(story["url"])

    for cat in CATEGORIES:
        pool = existing[cat]
        pool = [
            s for s in pool
            if s.get("published_date") and
               dateparser.parse(s["published_date"]).astimezone(timezone.utc) >= cutoff
        ]
        pool.sort(key=lambda s: s.get("published_date") or "", reverse=True)
        existing[cat] = pool[:MAX_PER_CATEGORY]

    return existing


def write_output(grouped: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    flat = []
    for stories in grouped.values():
        flat.extend(stories)
    output = {
        "country": "Syria",
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "story_count": len(flat),
        "stories": flat,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)
    log.info("Wrote %d stories to %s", len(flat), OUTPUT_FILE)


def main():
    log.info("Loading existing data ...")
    existing = load_existing()

    log.info("Fetching %d RSS feeds ...", len(FEEDS))
    fresh = []
    for cfg in FEEDS:
        results = fetch_feed(cfg)
        log.info("  %s — %d stories from %s", cfg["source"], len(results), cfg["url"])
        fresh.extend(results)
        time.sleep(0.5)  # polite crawl delay

    log.info("Merging %d fresh stories ...", len(fresh))
    merged = merge(existing, fresh)

    counts = {cat: len(merged[cat]) for cat in CATEGORIES}
    log.info("Category totals: %s", counts)

    write_output(merged)


if __name__ == "__main__":
    main()
