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
