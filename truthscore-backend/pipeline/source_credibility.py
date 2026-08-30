"""
TruthScore -- Per-Domain Source Credibility Tracker
=====================================================
Maintains a MongoDB collection (source_credibility) that accumulates
reliability statistics for every domain seen as a source across all
verifications. Used to surface credibility signals in the UI and to
weight sources in future pipeline runs.

Collection: source_credibility
Schema per doc:
    {
      "_id": "reuters.com",       # normalized domain (no www.)
      "domain": "reuters.com",
      "reliability_score": 87,    # 0-100 float
      "total_sources": 145,
      "supporting_count": 98,
      "contradicting_count": 12,
      "neutral_count": 35,
      "nli_scores": [...],         # last 50 NLI entailment scores
      "factcheck_count": 23,
      "academic_count": 18,
      "last_updated": "<ISO datetime>"
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_domain(url: str) -> str:
    """Extract normalized domain from a URL (strip www., lowercase).

    Returns "" on any error or if the input is empty.
    """
    try:
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        domain = (parsed.netloc or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        # strip port if present
        domain = domain.split(":")[0].strip()
        return domain
    except Exception:
        return ""


def _compute_reliability(
    supporting: int,
    contradicting: int,
    total: int,
    factcheck_count: int,
) -> float:
    """Clamp(50 + (sup-con)/max(total,1)*30 + factcheck/max(total,1)*20, 0, 100)."""
    denom = max(total, 1)
    score = (
        50
        + (supporting - contradicting) / denom * 30
        + factcheck_count / denom * 20
    )
    return round(max(0.0, min(100.0, score)), 2)


# ---------------------------------------------------------------------------
# Core async API
# ---------------------------------------------------------------------------

async def update_domain_stats(db, sources: list[dict], verdict: str) -> None:
    """Upsert credibility statistics for every domain in *sources*.

    For each source:
    - Extracts its domain via extract_domain().
    - Classifies it as supporting / contradicting / neutral based on
      source.get("stance") or source.get("retrieval_hint").
    - Appends the NLI entailment score (source["nli"]["entailment"]) to
      nli_scores, keeping the last 50.
    - Increments factcheck_count / academic_count based on source.get("type").
    - Recomputes reliability_score after every update.

    Entirely non-raising: all exceptions are silently swallowed.
    """
    try:
        if db is None or not sources:
            return

        col = db["source_credibility"]

        for source in sources:
            try:
                url = source.get("url") or ""
                domain = extract_domain(url)
                if not domain:
                    continue

                # --- stance bucket ---
                stance = (
                    source.get("stance")
                    or source.get("retrieval_hint")
                    or ""
                ).lower().strip()

                if stance in ("supporting", "support", "supports", "entails", "for"):
                    sup_inc, con_inc, neu_inc = 1, 0, 0
                elif stance in (
                    "contradicting", "contradicts", "refutes", "against", "contra",
                ):
                    sup_inc, con_inc, neu_inc = 0, 1, 0
                else:
                    sup_inc, con_inc, neu_inc = 0, 0, 1

                # --- NLI entailment score ---
                nli_score = None
                nli = source.get("nli")
                if isinstance(nli, dict):
                    raw = nli.get("entailment")
                    if raw is not None:
                        try:
                            nli_score = float(raw)
                        except (TypeError, ValueError):
                            pass

                # --- type counters ---
                src_type = (source.get("type") or "").lower().strip()
                factcheck_inc = 1 if src_type == "factcheck" else 0
                academic_inc  = 1 if src_type == "academic"  else 0

                # Fetch existing doc to recompute reliability_score with
                # the post-update totals (avoids relying on two-phase reads
                # that could race; good enough for best-effort credibility).
                existing = await col.find_one({"_id": domain}) or {}
                new_total = existing.get("total_sources", 0) + 1
                new_sup   = existing.get("supporting_count", 0) + sup_inc
                new_con   = existing.get("contradicting_count", 0) + con_inc
                new_fc    = existing.get("factcheck_count", 0) + factcheck_inc

                reliability = _compute_reliability(new_sup, new_con, new_total, new_fc)

                update_op: dict = {
                    "$set": {
                        "domain": domain,
                        "reliability_score": reliability,
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    },
                    "$inc": {
                        "total_sources":       1,
                        "supporting_count":    sup_inc,
                        "contradicting_count": con_inc,
                        "neutral_count":       neu_inc,
                        "factcheck_count":     factcheck_inc,
                        "academic_count":      academic_inc,
                    },
                }

                if nli_score is not None:
                    update_op["$push"] = {
                        "nli_scores": {
                            "$each":  [nli_score],
                            "$slice": -50,
                        }
                    }

                await col.update_one(
                    {"_id": domain},
                    update_op,
                    upsert=True,
                )
            except Exception:
                continue  # bad source — skip, keep going

    except Exception:
        pass  # never raise to caller


async def get_domain_score(db, domain: str) -> dict | None:
    """Return the source_credibility doc for *domain*, or None."""
    try:
        if db is None or not domain:
            return None
        doc = await db["source_credibility"].find_one({"_id": domain})
        return doc or None
    except Exception:
        return None


async def list_domains_for_sitemap(db, limit: int = 5000) -> list[dict]:
    """Return [{_id, last_updated}] for domains with enough evidence to index."""
    try:
        if db is None:
            return []
        cursor = (
            db["source_credibility"]
            .find({"total_sources": {"$gte": 5}}, {"_id": 1, "last_updated": 1})
            .sort("reliability_score", -1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)
    except Exception:
        return []


async def get_top_sources(db, limit: int = 20) -> list[dict]:
    """Return top reliable sources sorted by reliability_score desc.

    Only includes domains with at least 5 total_sources to avoid
    single-appearance outliers dominating the list.
    """
    try:
        if db is None:
            return []
        limit = max(1, int(limit))
        cursor = (
            db["source_credibility"]
            .find({"total_sources": {"$gte": 5}}, {"_id": 0})
            .sort("reliability_score", -1)
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
        return docs or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public SEO page renderer — /source/{domain}
# ---------------------------------------------------------------------------

def _rating_label(score: float) -> tuple[str, str]:
    """Return (label, color) for a reliability score."""
    if score >= 75:
        return "Highly Reliable", "#22c55e"
    if score >= 55:
        return "Generally Reliable", "#84cc16"
    if score >= 45:
        return "Mixed", "#f59e0b"
    if score >= 30:
        return "Questionable", "#f97316"
    return "Low Reliability", "#ef4444"


def render_source_page(doc: dict, base_url: str) -> str:
    """Generate a self-contained SEO HTML page for one source domain."""
    domain = doc.get("domain", doc.get("_id", ""))
    score = float(doc.get("reliability_score", 50))
    total = int(doc.get("total_sources", 0))
    sup = int(doc.get("supporting_count", 0))
    con = int(doc.get("contradicting_count", 0))
    neu = int(doc.get("neutral_count", 0))
    fc = int(doc.get("factcheck_count", 0))
    ac = int(doc.get("academic_count", 0))
    updated_at = (doc.get("last_updated") or "")[:10]
    page_url = f"{base_url}/source/{domain}"

    label, color = _rating_label(score)
    iscore = round(score)
    og_desc = f"{domain} reliability: {label} ({iscore}/100), based on {total:,} appearances as a source in TruthScore fact-checks."

    def _bar(name: str, val: int, c: str) -> str:
        pct = round(val / max(total, 1) * 100)
        return f"""<div class="stat-row">
  <span class="stat-name">{name}</span>
  <div class="stat-bar-bg"><div class="stat-bar-fg" style="width:{pct}%;background:{c}"></div></div>
  <span class="stat-val">{val:,} ({pct}%)</span>
</div>"""

    import json as _json
    jsonld = _json.dumps({
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": domain,
        "url": f"https://{domain}",
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": iscore,
            "bestRating": 100,
            "worstRating": 0,
            "ratingCount": total,
        },
    }, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{domain} — Source Reliability | TruthScore</title>
<meta name="description" content="{og_desc}">
<meta property="og:title" content="{domain} — {label} ({iscore}/100)">
<meta property="og:description" content="{og_desc}">
<meta property="og:url" content="{page_url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="TruthScore">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="{page_url}">
<script type="application/ld+json">{jsonld}</script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f17;color:#e2e8f0;line-height:1.6;min-height:100vh}}
.wrap{{max-width:720px;margin:0 auto;padding:24px 16px 60px}}
.nav{{display:flex;align-items:center;gap:12px;margin-bottom:28px;padding-bottom:16px;border-bottom:1px solid #2a2a3e}}
.logo{{font-size:18px;font-weight:700;color:#a78bfa;text-decoration:none}}
.nav-tag{{font-size:12px;color:#6b7280;background:#1e1e2e;padding:3px 8px;border-radius:99px}}
h1{{font-size:clamp(20px,4vw,30px);font-weight:700;color:#f1f5f9;margin-bottom:6px}}
.subttl{{font-size:13px;color:#6b7280;margin-bottom:24px}}
.rating-card{{background:#1a1a2e;border-radius:14px;padding:22px;margin-bottom:24px;text-align:center;border:1px solid #2a2a3e}}
.big-score{{font-size:54px;font-weight:800;color:{color};line-height:1}}
.big-score span{{font-size:22px;color:#6b7280}}
.rating-label{{font-size:16px;font-weight:700;color:{color};margin-top:6px}}
.rating-sub{{font-size:12px;color:#6b7280;margin-top:6px}}
.sec-hd{{font-size:14px;font-weight:600;color:#cbd5e1;margin:24px 0 12px}}
.stat-row{{display:flex;align-items:center;gap:10px;margin-bottom:10px}}
.stat-name{{font-size:13px;color:#cbd5e1;width:100px;flex-shrink:0}}
.stat-bar-bg{{flex:1;height:8px;border-radius:4px;background:#2a2a3e;overflow:hidden}}
.stat-bar-fg{{height:8px;border-radius:4px}}
.stat-val{{font-size:12px;color:#9ca3af;width:110px;text-align:right;flex-shrink:0}}
.badges{{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}}
.badge{{font-size:12px;color:#cbd5e1;background:#1e1e2e;padding:6px 12px;border-radius:8px}}
.disclaimer{{font-size:11px;color:#4b5563;margin-top:20px;font-style:italic}}
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
    <span class="nav-tag">Source Reliability</span>
  </nav>

  <h1>{domain}</h1>
  <div class="subttl">Credibility profile derived from real fact-check evidence</div>

  <div class="rating-card">
    <div class="big-score">{iscore}<span>/100</span></div>
    <div class="rating-label">{label}</div>
    <div class="rating-sub">Based on {total:,} appearance{"s" if total != 1 else ""} as a cited source{(" · updated " + updated_at) if updated_at else ""}</div>
  </div>

  <h2 class="sec-hd">Evidence breakdown</h2>
  {_bar("Supporting", sup, "#22c55e")}
  {_bar("Contradicting", con, "#ef4444")}
  {_bar("Neutral", neu, "#6b7280")}

  <div class="badges">
    <span class="badge">🔍 {fc:,} fact-check citations</span>
    <span class="badge">🎓 {ac:,} academic citations</span>
  </div>

  <p class="disclaimer">Reliability is computed automatically from how often this domain's content supported vs. contradicted claims that TruthScore verified against other evidence. It is a data-driven signal, not an editorial judgment of the publication.</p>

  <div class="cta">
    <p>Check any claim or article against real sources — instantly.</p>
    <a href="{base_url}">Try TruthScore →</a>
  </div>

  <footer>
    <p>TruthScore · AI fact-checking with real sources · <a href="{base_url}/privacy">Privacy</a></p>
  </footer>
</div>
</body>
</html>"""
