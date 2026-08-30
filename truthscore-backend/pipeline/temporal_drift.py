"""
TruthScore Temporal Truth Drift
=================================
Tracks how the truth value of a claim changes over time.

A claim that was TRUE in 2021 may be FALSE today (e.g., drug efficacy
that got revised, statistics that changed, policies that were repealed).

How it works:
1. Every verdict is saved to `verdict_history` collection (via record_verdict_snapshot, called from /verify)
2. This module RE-VERIFIES watched claims on a schedule
3. Returns a timeline: [{date, verdict, score, reason, changed: bool}]
4. If verdict changed, send notification to user

MongoDB collections used:
  - `verdict_history`: {claim_hash, claim, verdict, score, date, source_urls}
  - `watched_claims`: {user_id, claim, verdict_id} (already exists)
"""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone


def _claim_hash(claim: str) -> str:
    """Stable identifier for a claim text (first 16 hex chars of SHA-256)."""
    return hashlib.sha256(claim.strip().lower().encode()).hexdigest()[:16]


async def record_verdict_snapshot(
    db,
    claim: str,
    verdict: str,
    score: int,
    explanation: str,
    source_urls: list[str] | None = None,
) -> str:
    """
    Save a verdict snapshot to the temporal history.
    Returns the claim_hash (stable ID across time).
    """
    col = db["verdict_history"]
    claim_hash = _claim_hash(claim)
    now = datetime.now(timezone.utc)

    # Check if last snapshot has the same verdict (avoid duplicate entries)
    last = await col.find_one(
        {"claim_hash": claim_hash},
        sort=[("date", -1)],
    )
    if last and last.get("verdict") == verdict and last.get("score") == score:
        return claim_hash  # No change, don't duplicate

    await col.insert_one({
        "claim_hash": claim_hash,
        "claim": claim[:500],
        "verdict": verdict,
        "score": score,
        "explanation": explanation[:300],
        "source_urls": (source_urls or [])[:5],
        "date": now,
        "date_str": now.strftime("%Y-%m-%d"),
    })
    return claim_hash


async def get_truth_timeline(db, claim: str, limit: int = 20) -> list[dict]:
    """
    Get the full truth history for a claim, newest first.
    Returns list of {date, verdict, score, explanation, changed}.
    """
    col = db["verdict_history"]
    claim_hash = _claim_hash(claim)

    cursor = col.find(
        {"claim_hash": claim_hash},
        sort=[("date", -1)],
        limit=limit,
    )
    docs = await cursor.to_list(length=limit)

    timeline = []
    prev_verdict = None
    for doc in reversed(docs):  # oldest first for change detection
        verdict = doc.get("verdict", "UNCERTAIN")
        changed = prev_verdict is not None and verdict != prev_verdict
        timeline.append({
            "date": doc.get("date_str", str(doc.get("date", ""))[:10]),
            "verdict": verdict,
            "score": doc.get("score", 50),
            "explanation": doc.get("explanation", ""),
            "source_urls": doc.get("source_urls", []),
            "changed": changed,
        })
        prev_verdict = verdict

    return list(reversed(timeline))  # newest first for display


async def get_drift_summary(db, claim: str) -> dict | None:
    """
    Returns a summary of truth drift for a claim:
    {
      "has_drift": bool,
      "first_verdict": str,
      "first_date": str,
      "current_verdict": str,
      "current_date": str,
      "total_checks": int,
      "change_count": int,
      "timeline": [...]
    }
    Returns None if fewer than 2 snapshots exist.
    """
    timeline = await get_truth_timeline(db, claim)
    if len(timeline) < 2:
        return None

    oldest = timeline[-1]
    newest = timeline[0]
    change_count = sum(1 for t in timeline if t.get("changed"))

    return {
        "has_drift": oldest["verdict"] != newest["verdict"],
        "first_verdict": oldest["verdict"],
        "first_date": oldest["date"],
        "current_verdict": newest["verdict"],
        "current_date": newest["date"],
        "total_checks": len(timeline),
        "change_count": change_count,
        "timeline": timeline,
    }


async def scan_watched_for_drift(db) -> list[dict]:
    """
    Re-verify all watched claims that haven't been checked in 30+ days.
    Returns list of claims where verdict changed.

    Called by POST /temporal-drift/scan (admin endpoint).
    """
    from datetime import timedelta
    from pipeline.verify import verify_claim
    from models import VerifyRequest

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    watched_col = db["watched_claims"]
    history_col = db["verdict_history"]

    # Find watched claims where last check was > 30 days ago
    cursor = watched_col.find({})
    watched = await cursor.to_list(length=200)

    drifted = []
    for w in watched:
        claim = w.get("claim", "")
        if not claim:
            continue
        claim_hash = _claim_hash(claim)
        last = await history_col.find_one(
            {"claim_hash": claim_hash}, sort=[("date", -1)]
        )
        if last and last.get("date") and last["date"] > cutoff:
            continue  # Checked recently

        # Re-verify
        try:
            result = await verify_claim(VerifyRequest(text=claim))
            new_verdict = result.verdict
            old_verdict = last.get("verdict") if last else None

            await record_verdict_snapshot(
                db, claim, new_verdict, result.score,
                result.explanation or "",
                [s.url for s in (result.supporting or [])[:3] if s.url],
            )

            if old_verdict and old_verdict != new_verdict:
                drifted.append({
                    "claim": claim[:200],
                    "old_verdict": old_verdict,
                    "new_verdict": new_verdict,
                    "score": result.score,
                    "user_id": str(w.get("user_id", "")),
                })
        except Exception as e:
            print(f"[temporal_drift] re-verify error for '{claim[:60]}': {e}")

    return drifted
