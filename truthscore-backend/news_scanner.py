"""
TruthScore News Scanner
========================
Crawls RSS feeds from top news sources, extracts factual claims,
and auto-verifies the most newsworthy ones daily.

Results stored in MongoDB `daily_checks` collection, served at GET /today.

Required env vars:
  PUBLIC_BASE_URL — your backend URL (used for internal /verify calls)

Schedule: POST /news-scanner/run once daily (admin-only endpoint)
"""
import os
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

# Top RSS feeds to scan
RSS_FEEDS = [
    ("Reuters", "https://feeds.reuters.com/reuters/topNews"),
    ("AP News", "https://rsshub.app/apnews/topics/apf-topnews"),
    ("BBC News", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("Digi24", "https://www.digi24.ro/rss"),
    ("G4Media", "https://www.g4media.ro/feed"),
    ("HotNews", "https://www.hotnews.ro/rss"),
]

MAX_CLAIMS_PER_RUN = 10  # max claims to auto-verify per run (API cost control)
MAX_ITEMS_PER_FEED = 5   # max RSS items to read per feed


class _MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._data = []
    def handle_data(self, d):
        self._data.append(d)
    def get_data(self):
        return " ".join(self._data)


def strip_html(html: str) -> str:
    s = _MLStripper()
    s.feed(html or "")
    return s.get_data().strip()


def extract_rss_items(xml: str) -> list[dict]:
    """Parse RSS items from raw XML text."""
    items = []
    import re
    raw_items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    for raw in raw_items[:MAX_ITEMS_PER_FEED]:
        def tag(t):
            m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", raw, re.DOTALL)
            if m:
                return strip_html(m.group(1).strip())
            return ""
        title = tag("title")
        description = tag("description")
        link = tag("link")
        pub_date = tag("pubDate")
        if title and len(title) > 20:
            items.append({"title": title, "description": description, "link": link, "pub_date": pub_date})
    return items


async def fetch_feed(source: str, url: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "TruthScore-Scanner/1.0"})
            r.raise_for_status()
        items = extract_rss_items(r.text)
        for item in items:
            item["source"] = source
        return items
    except Exception as e:
        print(f"[scanner] feed {source} failed: {e}")
        return []


async def detect_claims_from_text(text: str) -> list[str]:
    """Call the local /detect-claims endpoint."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{PUBLIC_BASE_URL}/detect-claims",
                json={"text": text[:3000]},
            )
            r.raise_for_status()
            data = r.json()
            return [c["text"] if isinstance(c, dict) else c for c in (data.get("claims") or [])]
    except Exception:
        return []


async def verify_claim_text(claim: str) -> dict:
    """Call the local /verify endpoint."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{PUBLIC_BASE_URL}/verify", json={"text": claim})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"verdict": "UNCERTAIN", "score": 50, "error": str(e)}


async def run_scan(db) -> dict:
    """
    Fetch RSS feeds, extract claims, verify top ones, store results.
    Returns summary dict.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Check if already ran today
    col = db["daily_checks"]
    existing = await col.find_one({"date": today})
    if existing:
        return {"skipped": True, "reason": "already ran today", "date": today}

    # Fetch all feeds concurrently
    feed_results = await asyncio.gather(*[fetch_feed(src, url) for src, url in RSS_FEEDS])
    all_items = [item for feed in feed_results for item in feed]

    # Extract claims from headlines + descriptions
    claim_candidates: list[tuple[str, str, str]] = []  # (claim, source, link)
    seen: set[str] = set()
    for item in all_items[:30]:
        text = f"{item['title']}. {item.get('description', '')}".strip()
        claims = await detect_claims_from_text(text)
        for claim in claims[:2]:
            if claim not in seen and len(claim) > 20:
                seen.add(claim)
                claim_candidates.append((claim, item.get("source", ""), item.get("link", "")))
        if len(claim_candidates) >= MAX_CLAIMS_PER_RUN * 2:
            break

    # Verify top N claims
    checked = []
    for claim, source, link in claim_candidates[:MAX_CLAIMS_PER_RUN]:
        result = await verify_claim_text(claim)
        if not result.get("error"):
            checked.append({
                "claim": claim,
                "source": source,
                "source_link": link,
                "verdict": result.get("verdict", "UNCERTAIN"),
                "score": result.get("score", 50),
                "explanation": result.get("explanation", ""),
                "_verdictId": result.get("_verdictId", ""),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        await asyncio.sleep(1)  # gentle throttle

    doc = {
        "date": today,
        "items": checked,
        "feeds_scanned": len(RSS_FEEDS),
        "claims_found": len(claim_candidates),
        "claims_verified": len(checked),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await col.insert_one(doc)
    return {"date": today, "verified": len(checked), "candidates": len(claim_candidates)}
