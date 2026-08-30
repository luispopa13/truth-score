"""
TruthScore -- Trending Claims Module
=====================================
Surfaces trending/hot claims for the public "what's hot right now" page.

Tracks every claim that passes through the verification pipeline in a
dedicated `trending_claims` MongoDB collection, deduped by a SHA-256 hash
of the normalised claim text so identical claims from different users count
toward the same entry rather than spawning duplicates.

Public API
----------
  get_trending(db, limit)   -- top claims by check_count (min 2 checks)
  get_public_stats(db)      -- aggregate totals + top topics
  record_check(db, ...)     -- called by the pipeline after every verdict

All DB calls are wrapped in try/except so callers never see MongoDB errors;
fallbacks return empty lists / zero dicts instead of raising.
"""

import os
import hashlib
from datetime import datetime, timezone

# ── Collection name ──────────────────────────────────────────────────────────
_COLLECTION = "trending_claims"


# ── Normalisation & hash ──────────────────────────────────────────────────────

def _norm_hash(text: str) -> str:
    """Return a SHA-256 hex digest of the normalised claim text (lower, strip, [:300])."""
    normalised = (text or "").strip().lower()[:300]
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ── Core public functions ─────────────────────────────────────────────────────

async def record_check(
    db,
    claim: str,
    verdict: str,
    score: int,
    topic: str,
) -> None:
    """Upsert a check event into `trending_claims`.

    On first insert: sets claim text, first_seen, and all counters.
    On subsequent calls: increments check_count, updates last_* fields,
    increments the appropriate bucket counter, and recomputes false_ratio.

    Safe to call with a None/unavailable `db` — errors are swallowed.
    """
    if db is None:
        return

    try:
        col = db[_COLLECTION]
        doc_id = _norm_hash(claim)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Decide which verdict bucket to increment
        verdict_upper = (verdict or "UNCERTAIN").upper()
        if verdict_upper == "TRUE":
            bucket_inc = {"true_count": 1}
        elif verdict_upper == "FALSE":
            bucket_inc = {"false_count": 1}
        else:
            bucket_inc = {"uncertain_count": 1}

        # First perform the $inc + $set (for last_* and $setOnInsert for first_seen).
        # We must recompute false_ratio after, so we do a find_one_and_update
        # with return_document=True to get the updated doc.
        from pymongo import ReturnDocument

        updated = await col.find_one_and_update(
            {"_id": doc_id},
            {
                "$inc": {"check_count": 1, **bucket_inc},
                "$set": {
                    "last_verdict": verdict_upper,
                    "last_score": int(score),
                    "last_seen": now_iso,
                    "topic": topic or "general",
                },
                "$setOnInsert": {
                    "claim": (claim or "").strip()[:300],
                    "first_seen": now_iso,
                    "true_count": 0,
                    "false_count": 0,
                    "uncertain_count": 0,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

        # Recompute false_ratio from the post-update document
        if updated:
            check_count = updated.get("check_count", 1)
            false_count = updated.get("false_count", 0)
            false_ratio = false_count / check_count if check_count > 0 else 0.0
            await col.update_one(
                {"_id": doc_id},
                {"$set": {"false_ratio": round(false_ratio, 4)}},
            )

    except Exception as e:
        print(f"[TRENDING] record_check failed (non-fatal): {e}")


async def get_trending(db, limit: int = 10) -> list[dict]:
    """Return the top trending claims sorted by check_count descending.

    Filters out any entry with check_count < 2 (single-check claims are noise).
    Falls back to [] if MongoDB is unavailable.

    Each returned dict:
        claim, check_count, last_verdict, last_score, topic,
        first_seen, last_seen, false_ratio
    """
    if db is None:
        return []

    limit = max(1, min(int(limit or 10), 100))

    try:
        col = db[_COLLECTION]
        cursor = (
            col.find(
                {"check_count": {"$gte": 2}},
                {
                    "_id": 0,
                    "claim": 1,
                    "check_count": 1,
                    "last_verdict": 1,
                    "last_score": 1,
                    "topic": 1,
                    "first_seen": 1,
                    "last_seen": 1,
                    "false_ratio": 1,
                },
            )
            .sort("check_count", -1)
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
        return docs if docs else []
    except Exception as e:
        print(f"[TRENDING] get_trending failed (non-fatal): {e}")
        return []


async def get_public_stats(db) -> dict:
    """Aggregate totals across all trending_claims and the top topics.

    Returns:
        {
            total_checks:    int,
            total_true:      int,
            total_false:     int,
            total_uncertain: int,
            top_topics:      [{topic: str, count: int}, ...]
        }

    Returns zero values if MongoDB is unavailable.
    """
    _zero = {
        "total_checks": 0,
        "total_true": 0,
        "total_false": 0,
        "total_uncertain": 0,
        "top_topics": [],
    }

    if db is None:
        return _zero

    try:
        col = db[_COLLECTION]

        # Single aggregation pipeline: sum global counters + group by topic
        pipeline = [
            {
                "$facet": {
                    "totals": [
                        {
                            "$group": {
                                "_id": None,
                                "total_checks":    {"$sum": "$check_count"},
                                "total_true":      {"$sum": "$true_count"},
                                "total_false":     {"$sum": "$false_count"},
                                "total_uncertain": {"$sum": "$uncertain_count"},
                            }
                        }
                    ],
                    "topics": [
                        {
                            "$group": {
                                "_id": "$topic",
                                "count": {"$sum": "$check_count"},
                            }
                        },
                        {"$sort": {"count": -1}},
                        {"$limit": 10},
                        {"$project": {"_id": 0, "topic": "$_id", "count": 1}},
                    ],
                }
            }
        ]

        result = await col.aggregate(pipeline).to_list(length=1)
        if not result:
            return _zero

        facet = result[0]
        totals_list = facet.get("totals") or []
        totals = totals_list[0] if totals_list else {}
        top_topics = facet.get("topics") or []

        return {
            "total_checks":    int(totals.get("total_checks", 0)),
            "total_true":      int(totals.get("total_true", 0)),
            "total_false":     int(totals.get("total_false", 0)),
            "total_uncertain": int(totals.get("total_uncertain", 0)),
            "top_topics":      top_topics,
        }

    except Exception as e:
        print(f"[TRENDING] get_public_stats failed (non-fatal): {e}")
        return _zero
