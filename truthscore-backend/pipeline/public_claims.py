"""
TruthScore — Public Claim Pages
Every verified claim gets a permanent, SEO-indexed URL.
"""
import re
import hashlib
from datetime import datetime, timezone

# ── slug helpers ────────────────────────────────────────────

def make_slug(claim: str) -> str:
    """Create a stable URL-safe slug for a claim."""
    text = claim.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    text = text[:70].rstrip("-")
    suffix = hashlib.sha256(claim.encode()).hexdigest()[:7]
    return f"{text}-{suffix}"


def _verdict_label(verdict: str) -> str:
    v = (verdict or "").upper()
    if v in ("TRUE", "MOSTLY_TRUE", "MOSTLY TRUE"):
        return "TRUE"
    if v in ("FALSE", "MOSTLY_FALSE", "MOSTLY FALSE"):
        return "FALSE"
    return "UNCERTAIN"


def _verdict_color(verdict: str) -> str:
    v = _verdict_label(verdict)
    return {"TRUE": "#22c55e", "FALSE": "#ef4444"}.get(v, "#f59e0b")


def _score_bar_color(score: int) -> str:
    if score >= 70:
        return "#22c55e"
    if score >= 40:
        return "#f59e0b"
    return "#ef4444"


# ── DB helpers ──────────────────────────────────────────────

async def upsert_public_claim(
    db,
    claim: str,
    verdict: str,
    score: int,
    sources: list,
    explanation: str = "",
    topic: str = "",
) -> str:
    """Create or update the public claim record. Returns the slug."""
    try:
        slug = make_slug(claim)
        now = datetime.now(timezone.utc).isoformat()

        src_cards = []
        for s in (sources or [])[:24]:
            if isinstance(s, dict):
                src_cards.append({
                    "url": s.get("url", ""),
                    "publisher": s.get("publisher", ""),
                    "title": s.get("title", ""),
                    "snippet": s.get("snippet", ""),
                    "stance": s.get("stance", ""),
                    "claim_index": s.get("claim_index", -1),
                })

        await db.public_claims.update_one(
            {"_id": slug},
            {
                "$set": {
                    "claim": claim,
                    "verdict": _verdict_label(verdict),
                    "score": score,
                    "explanation": explanation,
                    "topic": topic,
                    "sources": src_cards,
                    "updated_at": now,
                },
                "$inc": {"check_count": 1},
                "$setOnInsert": {"created_at": now, "_id": slug},
            },
            upsert=True,
        )
        return slug
    except Exception as e:
        print(f"[PUBLIC-CLAIMS] upsert error: {e}")
        return ""


async def get_public_claim(db, slug: str) -> dict | None:
    try:
        return await db.public_claims.find_one({"_id": slug})
    except Exception:
        return None


async def list_slugs_for_sitemap(db, limit: int = 5000) -> list[dict]:
    """Return [{slug, updated_at}] for sitemap generation."""
    try:
        cursor = db.public_claims.find(
            {}, {"_id": 1, "updated_at": 1}
        ).sort("updated_at", -1).limit(limit)
        return await cursor.to_list(length=limit)
    except Exception:
        return []


# ── HTML page renderer ──────────────────────────────────────

def render_claim_page(doc: dict, base_url: str) -> str:
    """Generate a fully self-contained SEO HTML page for a claim."""
    slug = doc.get("_id", "")
    claim = doc.get("claim", "")
    verdict = doc.get("verdict", "UNCERTAIN")
    score = int(doc.get("score", 50))
    explanation = doc.get("explanation", "")
    topic = doc.get("topic", "")
    check_count = int(doc.get("check_count", 1))
    updated_at = (doc.get("updated_at") or "")[:10]
    sources = doc.get("sources") or []
    page_url = f"{base_url}/claim/{slug}"

    vlabel = _verdict_label(verdict)
    vcolor = _verdict_color(verdict)
    scolor = _score_bar_color(score)

    verdict_emoji = {"TRUE": "✅", "FALSE": "❌"}.get(vlabel, "⚠️")

    # ── source cards HTML ──
    supporting = [s for s in sources if s.get("stance", "").upper() in ("SUPPORTING", "SUPPORTS", "SUPPORT")]
    contradicting = [s for s in sources if s.get("stance", "").upper() in ("CONTRADICTING", "CONTRADICTS", "CONTRADICT")]
    neutral = [s for s in sources if s.get("stance", "").upper() not in
               ("SUPPORTING", "SUPPORTS", "SUPPORT", "CONTRADICTING", "CONTRADICTS", "CONTRADICT")]

    def _src_html(s: dict, color: str) -> str:
        pub = s.get("publisher") or ""
        title = (s.get("title") or "")[:90]
        snippet = (s.get("snippet") or "")[:200]
        url = s.get("url") or "#"
        ci = s.get("claim_index", -1)
        badge = f'<span class="ci-badge">sub-claim #{ci + 1}</span>' if ci >= 0 else ""
        snip_html = f'<p class="snip">"{snippet}"</p>' if snippet else ""
        return f"""<div class="src-card" style="border-left:3px solid {color}">
  <a href="{url}" target="_blank" rel="noopener noreferrer" class="src-title">{title or pub or url}</a>
  {snip_html}
  <span class="src-pub">{pub}</span>{badge}
</div>"""

    src_sections = ""
    if supporting:
        cards = "".join(_src_html(s, "#22c55e") for s in supporting[:8])
        src_sections += f'<h3 class="src-grp-hd" style="color:#22c55e">✓ Supports ({len(supporting)})</h3><div class="src-group">{cards}</div>'
    if contradicting:
        cards = "".join(_src_html(s, "#ef4444") for s in contradicting[:8])
        src_sections += f'<h3 class="src-grp-hd" style="color:#ef4444">✗ Contradicts ({len(contradicting)})</h3><div class="src-group">{cards}</div>'
    if neutral and not (supporting or contradicting):
        cards = "".join(_src_html(s, "#6b7280") for s in neutral[:6])
        src_sections += f'<h3 class="src-grp-hd">Sources</h3><div class="src-group">{cards}</div>'

    topic_meta = f" | {topic}" if topic else ""
    og_desc = f"{verdict_emoji} {vlabel} (score {score}/100) — {explanation[:140]}" if explanation else f"{verdict_emoji} {vlabel} — score {score}/100"

    # JSON-LD ClaimReview (Google rich result for fact-checks)
    import json as _json
    jsonld = _json.dumps({
        "@context": "https://schema.org",
        "@type": "ClaimReview",
        "url": page_url,
        "claimReviewed": claim,
        "datePublished": updated_at,
        "author": {"@type": "Organization", "name": "TruthScore", "url": base_url},
        "reviewRating": {
            "@type": "Rating",
            "ratingValue": score,
            "bestRating": 100,
            "worstRating": 0,
            "alternateName": vlabel,
        },
        "itemReviewed": {
            "@type": "Claim",
            "name": claim,
        },
    }, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{claim[:100]} — TruthScore Fact Check</title>
<meta name="description" content="{og_desc}">
<meta property="og:title" content="{verdict_emoji} {claim[:90]} — TruthScore">
<meta property="og:description" content="{og_desc}">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="TruthScore">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{verdict_emoji} {claim[:90]}">
<meta name="twitter:description" content="{og_desc}">
<link rel="canonical" href="{page_url}">
<script type="application/ld+json">{jsonld}</script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f17;color:#e2e8f0;line-height:1.6;min-height:100vh}}
.wrap{{max-width:760px;margin:0 auto;padding:24px 16px 60px}}
.nav{{display:flex;align-items:center;gap:12px;margin-bottom:32px;padding-bottom:16px;border-bottom:1px solid #2a2a3e}}
.logo{{font-size:18px;font-weight:700;color:#a78bfa;text-decoration:none}}
.nav-tag{{font-size:12px;color:#6b7280;background:#1e1e2e;padding:3px 8px;border-radius:99px}}
h1{{font-size:clamp(18px,3vw,26px);font-weight:700;color:#f1f5f9;margin-bottom:20px;line-height:1.35}}
.verdict-row{{display:flex;align-items:center;gap:14px;margin-bottom:20px;flex-wrap:wrap}}
.v-chip{{font-size:15px;font-weight:700;padding:6px 16px;border-radius:99px;color:#fff;background:{vcolor}}}
.score-wrap{{flex:1;min-width:140px}}
.score-lbl{{font-size:12px;color:#9ca3af;margin-bottom:4px}}
.score-bar-bg{{height:8px;border-radius:4px;background:#2a2a3e;overflow:hidden}}
.score-bar-fg{{height:8px;border-radius:4px;background:{scolor};width:{score}%}}
.score-num{{font-size:13px;color:{scolor};font-weight:600;margin-top:4px}}
.meta{{font-size:12px;color:#6b7280;margin-bottom:20px}}
.meta span{{margin-right:14px}}
.explanation{{background:#1e1e2e;border-radius:10px;padding:16px;margin-bottom:24px;font-size:14px;color:#cbd5e1;border-left:3px solid #a78bfa}}
.src-grp-hd{{font-size:13px;font-weight:600;margin:18px 0 8px}}
.src-group{{display:flex;flex-direction:column;gap:8px;margin-bottom:8px}}
.src-card{{background:#1a1a2e;border-radius:8px;padding:10px 12px}}
.src-title{{font-size:13px;font-weight:500;color:#a78bfa;text-decoration:none;display:block;margin-bottom:4px}}
.src-title:hover{{text-decoration:underline}}
.snip{{font-size:12px;color:#94a3b8;font-style:italic;margin-bottom:4px;border-left:2px solid #374151;padding-left:8px}}
.src-pub{{font-size:11px;color:#6b7280}}
.ci-badge{{font-size:10px;color:#a78bfa;background:#2a2a3e;padding:1px 6px;border-radius:99px;margin-left:6px}}
.cta{{margin-top:36px;padding:20px;background:linear-gradient(135deg,#1e1e2e,#2a1f3d);border-radius:12px;border:1px solid #3730a3;text-align:center}}
.cta p{{font-size:14px;color:#94a3b8;margin-bottom:12px}}
.cta a{{display:inline-block;padding:10px 24px;background:#6d28d9;color:#fff;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none}}
.cta a:hover{{background:#7c3aed}}
footer{{margin-top:40px;text-align:center;font-size:12px;color:#4b5563}}
</style>
</head>
<body>
<div class="wrap">
  <nav class="nav">
    <a class="logo" href="{base_url}">TruthScore</a>
    <span class="nav-tag">Fact Check</span>
    {"<span class='nav-tag'>"+topic+"</span>" if topic else ""}
  </nav>

  <h1>{claim}</h1>

  <div class="verdict-row">
    <span class="v-chip">{verdict_emoji} {vlabel}</span>
    <div class="score-wrap">
      <div class="score-lbl">Truth Score</div>
      <div class="score-bar-bg"><div class="score-bar-fg"></div></div>
      <div class="score-num">{score}/100</div>
    </div>
  </div>

  <div class="meta">
    <span>Checked {check_count:,} time{"s" if check_count != 1 else ""}</span>
    {"<span>Last updated: " + updated_at + "</span>" if updated_at else ""}
    {"<span>Topic: " + topic + "</span>" if topic else ""}
  </div>

  {"<div class='explanation'>" + explanation + "</div>" if explanation else ""}

  {src_sections if sources else ""}

  <div class="cta">
    <p>Verify your own claims with AI-powered, source-backed fact-checking.</p>
    <a href="{base_url}">Check a claim on TruthScore →</a>
  </div>

  <footer>
    <p>TruthScore · AI fact-checking with real sources · <a href="{base_url}/privacy" style="color:#6b7280">Privacy</a></p>
  </footer>
</div>
</body>
</html>"""
