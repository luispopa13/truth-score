"""
TruthScore -- URL Fact-Checker
Fetches a URL, extracts readable text, and runs the standard analysis pipeline.
"""
import re
import httpx
from fastapi import HTTPException


# ── HTML entity decoder ──────────────────────────────────────────────────────

_HTML_ENTITIES = {
    "&amp;":  "&",
    "&lt;":   "<",
    "&gt;":   ">",
    "&quot;": '"',
    "&apos;": "'",
    "&#39;":  "'",
    "&nbsp;": " ",
}

def _decode_entities(text: str) -> str:
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    # Numeric decimal entities: &#160; &#8212; etc.
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    return text


# ── Text extraction ──────────────────────────────────────────────────────────

def _extract_text(html: str) -> tuple[str, str]:
    """Return (title, body_text) from raw HTML.

    No third-party parser — pure regex as required.
    """
    # Extract <title> before stripping tags
    title = ""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_m:
        title = _decode_entities(re.sub(r"<[^>]+>", " ", title_m.group(1))).strip()
        title = " ".join(title.split())[:200]

    # Remove blocks that contain no readable text
    for tag in ("script", "style", "head", "noscript", "svg", "iframe"):
        html = re.sub(
            rf"<{tag}[\s>].*?</{tag}>",
            " ",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)

    # Decode HTML entities
    text = _decode_entities(text)

    # Collapse whitespace (tabs, newlines, multiple spaces → single space)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Limit to 5000 characters for pipeline
    return title, text[:5000]


# ── Aggregate verdict helper ─────────────────────────────────────────────────

def _aggregate(results: list[dict]) -> tuple[str, int]:
    """Return (verdict, avg_score) from a list of VerifyResponse dicts."""
    if not results:
        return "UNCERTAIN", 50
    scores = [r.get("score", 50) for r in results]
    avg = round(sum(scores) / len(scores))
    if avg >= 70:
        verdict = "TRUE"
    elif avg <= 30:
        verdict = "FALSE"
    else:
        verdict = "UNCERTAIN"
    return verdict, avg


# ── Main exported function ───────────────────────────────────────────────────

async def check_url(url: str, db, user=None) -> dict:
    """Fetch *url*, extract text, and run the fact-check pipeline on it.

    Returns a dict with: url, title, text_preview, claim_count, results,
    verdict, score.

    Raises HTTPException on bad input or network/extraction failures.
    """
    # ── 1. Validate URL scheme ────────────────────────────────────────────
    if not url or not re.match(r"^https?://", url, re.IGNORECASE):
        raise HTTPException(
            status_code=400,
            detail="Invalid URL. Only http:// and https:// URLs are supported.",
        )

    # ── 2. Fetch the URL ─────────────────────────────────────────────────
    MAX_BYTES = 512_000  # 500 KB hard cap

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "TruthScoreBot/1.0"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            raw_bytes = response.content[:MAX_BYTES]

        # Decode — honour charset from Content-Type, fall back to utf-8
        content_type = response.headers.get("content-type", "")
        charset_m = re.search(r"charset=([^\s;]+)", content_type, re.IGNORECASE)
        charset = charset_m.group(1).strip('"') if charset_m else "utf-8"
        try:
            html = raw_bytes.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            html = raw_bytes.decode("utf-8", errors="replace")

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="URL timed out")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {exc}")

    # ── 3. Extract text ───────────────────────────────────────────────────
    title, text = _extract_text(html)

    # ── 4. Guard: require meaningful content ──────────────────────────────
    if len(text.strip()) < 50:
        raise HTTPException(
            status_code=422,
            detail="Could not extract meaningful text from URL",
        )

    # ── 5. Run the analysis pipeline ─────────────────────────────────────
    try:
        from pipeline.helpers import split_claims
        from pipeline.verify import verify_claim
        from models import VerifyRequest

        claims = await split_claims(text)
        claims = [c for c in claims if c and len(c.strip()) >= 5]
        # Cap at 5 claims to control cost / latency
        claims_to_verify = claims[:5]

        verified: list[dict] = []
        for claim_text in claims_to_verify:
            try:
                req = VerifyRequest(text=claim_text[:4000])
                result = await verify_claim(req)
                verified.append(result.model_dump())
            except Exception as _claim_err:
                # One failing claim must not abort the whole URL check
                print(f"  [URL-CHECK] claim failed: {_claim_err}")

        verdict, score = _aggregate(verified)

        return {
            "url":          url,
            "title":        title,
            "text_preview": text[:200],
            "claim_count":  len(claims_to_verify),
            "results":      verified,
            "verdict":      verdict,
            "score":        score,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Analysis pipeline error: {str(exc)[:200]}",
        )
