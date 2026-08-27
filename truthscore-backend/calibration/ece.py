"""
TruthScore -- ECE Calibration and Feedback Loop.
"""
from config import *

import os
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone

_feedback_store: list = []

# ── Durable feedback persistence (JSONL source of truth + optional Mongo) ──
# Mirrors pipeline/case_study.py conventions: append-only JSONL guarded by an
# async write lock, with a best-effort MongoDB mirror. The in-memory
# _feedback_store above stays as the live calibration state used by the ECE
# curve / weak-domain analysis; this block adds the durable copy so feedback
# survives restarts (and cloud redeploys where the local FS is ephemeral).
_BACKEND_ROOT = Path(__file__).parent.parent
_FEEDBACK_DIR = _BACKEND_ROOT / "data" / "feedback"
_FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

FEEDBACK_FILE = _FEEDBACK_DIR / "feedback.jsonl"

_write_lock_f = asyncio.Lock()

_mongo_collection_f = None
_mongo_init_tried = False


def _try_init_mongo():
    """Best-effort MongoDB setup. Silently does nothing if unavailable.

    Uses the SAME env vars as auth.py / case_study.py (MONGODB_URL /
    MONGODB_DB) so a single connection string covers everything and the
    durable copy survives cloud redeploys (ephemeral local filesystem).
    """
    global _mongo_collection_f, _mongo_init_tried
    if _mongo_init_tried:
        return
    _mongo_init_tried = True
    mongo_url = os.getenv("MONGODB_URL", "")
    db_name   = os.getenv("MONGODB_DB", "truthscore")
    if not mongo_url:
        return
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=2000)
        db = client[db_name]
        _mongo_collection_f = db["calibration_feedback"]
        print(f"[FEEDBACK] MongoDB mirror enabled (db={db_name})")
    except Exception as e:
        print(f"[FEEDBACK] MongoDB mirror disabled ({e}) -- using JSONL only")


async def record_feedback_durable(claim: str, verdict: str, score: int,
                                  topic: str, correct: bool,
                                  failure_reason: str = "") -> None:
    """Durably persist one piece of calibration feedback.

    Appends a JSON line to data/feedback/feedback.jsonl (under an async write
    lock) and best-effort mirrors it to MongoDB. The JSONL write always runs;
    the Mongo write is skipped gracefully when no client is configured.
    Does NOT touch the in-memory _feedback_store -- callers should update that
    separately via record_feedback() so the live ECE state stays in sync.
    """
    _try_init_mongo()
    record = {
        "claim": claim[:200] if claim else "",
        "verdict": verdict,
        "score": score,
        "topic": topic,
        "correct": correct,
        "failure_reason": failure_reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    line = json.dumps(record, ensure_ascii=False, default=str)
    async with _write_lock_f:
        try:
            with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            print(f"[FEEDBACK] Failed to write feedback log: {e}")

    if _mongo_collection_f is not None:
        try:
            await _mongo_collection_f.insert_one(dict(record))
        except Exception as e:
            print(f"[FEEDBACK] Mongo insert failed (non-fatal): {e}")


def compute_ece(results_path: str, n_bins: int = 10) -> dict:
    import csv
    rows = list(csv.DictReader(open(results_path, encoding="utf-8")))
    if not rows:
        return {"ece": None, "error": "empty file"}
    bins  = [[] for _ in range(n_bins)]
    total = 0
    for r in rows:
        try:
            score   = int(r.get("score", 50)) / 100.0
            correct = r.get("correct", "").upper() in ("YES", "TRUE")
            bin_idx = min(int(score * n_bins), n_bins - 1)
            bins[bin_idx].append((score, correct))
            total += 1
        except (ValueError, KeyError):
            pass
    if total == 0:
        return {"ece": None, "error": "no valid rows"}
    ece       = 0.0
    bin_stats = []
    for b in bins:
        if not b:
            continue
        avg_conf = sum(s for s, _ in b) / len(b)
        avg_acc  = sum(1 for _, c in b if c) / len(b)
        weight   = len(b) / total
        ece     += weight * abs(avg_conf - avg_acc)
        bin_stats.append({"n": len(b), "conf": round(avg_conf, 3),
                          "acc": round(avg_acc, 3)})
    return {
        "ece":     round(ece, 4),
        "total":   total,
        "bins":    bin_stats,
        "verdict": ("well-calibrated" if ece < 0.05
                    else "slightly overconfident" if ece < 0.10
                    else "overconfident"),
    }


def compute_calibration_map(results_path: str) -> dict:
    import csv
    from collections import defaultdict
    rows = list(csv.DictReader(open(results_path, encoding="utf-8")))
    bins = defaultdict(list)
    for r in rows:
        try:
            score  = int(r.get("score", 50))
            correct = r.get("correct", "").upper() in ("YES", "TRUE")
            bins[(score // 10) * 10].append(correct)
        except (ValueError, KeyError):
            pass
    cal_map = {}
    for bucket, results in sorted(bins.items()):
        accuracy = sum(results) / len(results) * 100
        cal_map[bucket] = round(accuracy)
    return cal_map


def record_feedback(claim: str, verdict: str, score: int,
                    topic: str, correct: bool,
                    failure_reason: str = "") -> None:
    import time
    entry = {
        "claim": claim[:200], "verdict": verdict,
        "score": score, "topic": topic,
        "correct": correct, "failure_reason": failure_reason,
        "timestamp": time.time(),
    }
    _feedback_store.append(entry)
    print(f"  [FEEDBACK] verdict={verdict} correct={correct} topic={topic}")


def get_weak_domains() -> list:
    from collections import defaultdict
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for entry in _feedback_store:
        t = entry.get("topic", "general")
        stats[t]["total"] += 1
        if entry.get("correct"):
            stats[t]["correct"] += 1
    results = []
    for topic, s in stats.items():
        if s["total"] >= 5:
            acc = s["correct"] / s["total"]
            results.append((topic, round(acc, 3)))
    return sorted(results, key=lambda x: x[1])