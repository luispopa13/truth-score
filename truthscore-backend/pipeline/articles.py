"""
TruthScore — Public Article Pages
Every fact-checked URL becomes a permanent, SEO-indexed page at /article/{slug}.
"""
import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse


# ── slug helpers ────────────────────────────────────────────

def make_article_slug(url: str, title: str = "") -> str:
    """Stable URL-safe slug from the source domain + title (or URL)."""
    domain = ""
    try:
        domain = (urlparse(url).netloc or "").lower().replace("www.", "")
    except Exception:
        pass
    base = (title or domain or url).lower().strip()
    base = re.sub(r"[^a-z0-9\s-]", "", base)
    base = re.sub(r"\s+", "-", base).strip("-")[:70].rstrip("-")
    suffix = hashlib.sha256(url.encode()).hexdigest()[:7]
    dom_prefix = re.sub(r"[^a-z0-9]", "-", domain).strip("-")
    slug = f"{dom_prefix}-{base}-{suffix}" if dom_prefix else f"{base}-{suffix}"
    return re.sub(r"-{2,}", "-", slug).strip("-")


def _verdict_label(verdict: str) -> str:
    v = (verdict or "").upper()
    if v in ("TRUE", "MOSTLY_TRUE", "MOSTLY TRUE"):
        return "TRUE"
    if v in ("FALSE", "MOSTLY_FALSE", "MOSTLY FALSE"):
        return "FALSE"
    return "UNCERTAIN"


def _verdict_color(verdict: str) -> str:
    return {"TRUE": "#22c55e", "FALSE": "#ef4444"}.get(_verdict_label(verdict), "#f59e0b")


def _score_color(score: int) -> str:
    if score >= 70:
        return "#22c55e"
    if score >= 40:
        return "#f59e0b"
    return "#ef4444"


# ── share text (viral copy) ─────────────────────────────────

def make_share_text(title: str, verdict: str, score: int, url: str) -> str:
    """Copy-ready social post summarizing the fact-check result."""
    v = _verdict_label(verdict)
    emoji = {"TRUE": "✅", "FALSE": "❌"}.get(v, "⚠️")
    label = {"TRUE": "checks out", "FALSE": "is misleading", "UNCERTAIN": "is unproven"}.get(v, "is unproven")
    subject = (title or url or "This article")[:90]
    return f'{emoji} «{subject}» {label} — TruthScore rates it {score}/100. Verified against real sources.'


# ── DB helpers ──────────────────────────────────────────────

async def upsert_article(
    db,
    url: str,
    title: str,
    verdict: str,
    score: int,
    results: list,
    text_preview: str = "",
) -> str:
    """Store the fact-checked article and return its slug."""
    try:
        slug = make_article_slug(url, title)
        now = datetime.now(timezone.utc).isoformat()
        domain = ""
        try:
            domain = (urlparse(url).netloc or "").lower().replace("www.", "")
        except Exception:
            pass

        claim_cards = []
        for r in (results or [])[:12]:
            if isinstance(r, dict):
                claim_cards.append({
                    "claim":       r.get("claim", ""),
                    "verdict":     _verdict_label(r.get("verdict", "")),
                    "score":       int(r.get("score", 50)),
                    "explanation": (r.get("explanation") or "")[:400],
                })

        await db.articles.update_one(
            {"_id": slug},
            {
                "$set": {
                    "url":          url,
                    "domain":       domain,
                    "title":        title,
                    "verdict":      _verdict_label(verdict),
                    "score":        score,
                    "claims":       claim_cards,
                    "text_preview": text_preview[:300],
                    "updated_at":   now,
                },
                "$inc": {"check_count": 1},
                "$setOnInsert": {"created_at": now, "_id": slug},
            },
            upsert=True,
        )
        return slug
    except Exception as e:
        print(f"[ARTICLES] upsert error: {e}")
        return ""


async def get_article(db, slug: str) -> dict | None:
    try:
        return await db.articles.find_one({"_id": slug})
    except Exception:
        return None


async def list_article_slugs_for_sitemap(db, limit: int = 5000) -> list[dict]:
    """Return [{_id, updated_at}] for sitemap generation."""
    try:
        cursor = db.articles.find(
            {}, {"_id": 1, "updated_at": 1}
        ).sort("updated_at", -1).limit(limit)
        return await cursor.to_list(length=limit)
    except Exception:
        return []


# ── HTML page renderer ──────────────────────────────────────

def render_article_page(doc: dict, base_url: str) -> str:
    slug = doc.get("_id", "")
    url = doc.get("url", "")
    domain = doc.get("domain", "")
    title = doc.get("title", "") or url
    verdict = doc.get("verdict", "UNCERTAIN")
    score = int(doc.get("score", 50))
    claims = doc.get("claims") or []
    check_count = int(doc.get("check_count", 1))
    updated_at = (doc.get("updated_at") or "")[:10]
    page_url = f"{base_url}/article/{slug}"

    vlabel = _verdict_label(verdict)
    vcolor = _verdict_color(verdict)
    scolor = _score_color(score)
    vemoji = {"TRUE": "✅", "FALSE": "❌"}.get(vlabel, "⚠️")

    def _claim_html(c: dict) -> str:
        cv = _verdict_label(c.get("verdict", ""))
        cvc = _verdict_color(cv)
        cs = int(c.get("score", 50))
        ce = {"TRUE": "✅", "FALSE": "❌"}.get(cv, "⚠️")
        expl = (c.get("explanation") or "")[:300]
        expl_html = f'<p class="c-expl">{expl}</p>' if expl else ""
        return f"""<div class="claim-card">
  <div class="claim-head">
    <span class="c-chip" style="background:{cvc}">{ce} {cv}</span>
    <span class="c-score" style="color:{_score_color(cs)}">{cs}/100</span>
  </div>
  <p class="c-text">{c.get('claim','')}</p>
  {expl_html}
</div>"""

    claims_html = "".join(_claim_html(c) for c in claims)
    og_desc = f"{vemoji} {vlabel} (score {score}/100) — TruthScore fact-checked {check_count:,} claim{'s' if len(claims)!=1 else ''} in this article."

    import json as _json
    jsonld = _json.dumps({
        "@context": "https://schema.org",
        "@type": "ClaimReview",
        "url": page_url,
        "claimReviewed": title[:200],
        "datePublished": updated_at,
        "author": {"@type": "Organization", "name": "TruthScore", "url": base_url},
        "reviewRating": {
            "@type": "Rating", "ratingValue": score,
            "bestRating": 100, "worstRating": 0, "alternateName": vlabel,
        },
        "itemReviewed": {"@type": "CreativeWork", "url": url, "name": title[:200]},
    }, ensure_ascii=False)

    domain_link = f'<a class="src-domain" href="{base_url}/source/{domain}">{domain}</a>' if domain else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title[:90]} — TruthScore Fact Check</title>
<meta name="description" content="{og_desc}">
<meta property="og:title" content="{vemoji} {title[:80]} — TruthScore">
<meta property="og:description" content="{og_desc}">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="TruthScore">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="{page_url}">
<script type="application/ld+json">{jsonld}</script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f17;color:#e2e8f0;line-height:1.6;min-height:100vh}}
.wrap{{max-width:760px;margin:0 auto;padding:24px 16px 60px}}
.nav{{display:flex;align-items:center;gap:12px;margin-bottom:28px;padding-bottom:16px;border-bottom:1px solid #2a2a3e}}
.logo{{font-size:18px;font-weight:700;color:#a78bfa;text-decoration:none}}
.nav-tag{{font-size:12px;color:#6b7280;background:#1e1e2e;padding:3px 8px;border-radius:99px}}
h1{{font-size:clamp(18px,3vw,26px);font-weight:700;color:#f1f5f9;margin-bottom:8px;line-height:1.35}}
.src-line{{font-size:13px;color:#6b7280;margin-bottom:20px}}
.src-line a{{color:#a78bfa;text-decoration:none}}
.verdict-row{{display:flex;align-items:center;gap:14px;margin-bottom:24px;flex-wrap:wrap}}
.v-chip{{font-size:15px;font-weight:700;padding:6px 16px;border-radius:99px;color:#fff;background:{vcolor}}}
.score-wrap{{flex:1;min-width:140px}}
.score-lbl{{font-size:12px;color:#9ca3af;margin-bottom:4px}}
.score-bar-bg{{height:8px;border-radius:4px;background:#2a2a3e;overflow:hidden}}
.score-bar-fg{{height:8px;border-radius:4px;background:{scolor};width:{score}%}}
.score-num{{font-size:13px;color:{scolor};font-weight:600;margin-top:4px}}
.sec-hd{{font-size:14px;font-weight:600;color:#cbd5e1;margin:24px 0 10px}}
.claim-card{{background:#1a1a2e;border-radius:10px;padding:14px;margin-bottom:10px}}
.claim-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}}
.c-chip{{font-size:12px;font-weight:700;padding:3px 10px;border-radius:99px;color:#fff}}
.c-score{{font-size:13px;font-weight:600}}
.c-text{{font-size:14px;color:#e2e8f0;margin-bottom:6px}}
.c-expl{{font-size:12px;color:#94a3b8}}
.cta{{margin-top:36px;padding:20px;background:linear-gradient(135deg,#1e1e2e,#2a1f3d);border-radius:12px;border:1px solid #3730a3;text-align:center}}
.cta p{{font-size:14px;color:#94a3b8;margin-bottom:12px}}
.cta a{{display:inline-block;padding:10px 24px;background:#6d28d9;color:#fff;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none}}
footer{{margin-top:40px;text-align:center;font-size:12px;color:#4b5563}}
footer a{{color:#6b7280}}
</style>
</head>
<body>
<div class="wrap">
  <nav class="nav">
    <a class="logo" href="{base_url}">TruthScore</a>
    <span class="nav-tag">Article Fact Check</span>
  </nav>

  <h1>{title}</h1>
  <div class="src-line">Source: <a href="{url}" target="_blank" rel="noopener nofollow">{url[:80]}</a> {("· " + domain_link) if domain_link else ""}</div>

  <div class="verdict-row">
    <span class="v-chip">{vemoji} {vlabel}</span>
    <div class="score-wrap">
      <div class="score-lbl">Overall Truth Score</div>
      <div class="score-bar-bg"><div class="score-bar-fg"></div></div>
      <div class="score-num">{score}/100 · checked {check_count:,}×{(" · " + updated_at) if updated_at else ""}</div>
    </div>
  </div>

  {"<h2 class='sec-hd'>Claims we checked</h2>" + claims_html if claims else ""}

  <div class="cta">
    <p>Paste any article URL and get an instant, source-backed fact-check.</p>
    <a href="{base_url}">Fact-check a URL on TruthScore →</a>
  </div>

  <footer>
    <p>TruthScore · AI fact-checking with real sources · <a href="{base_url}/privacy">Privacy</a></p>
  </footer>
</div>
</body>
</html>"""
