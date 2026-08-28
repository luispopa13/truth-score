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
                                  failure_reason: str = "",
                                  interaction_id: str = "") -> None:
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
        "interaction_id": interaction_id or "",
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
    with open(results_path, encoding="utf-8") as _f:
        rows = list(csv.DictReader(_f))
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
    with open(results_path, encoding="utf-8") as _f:
        rows = list(csv.DictReader(_f))
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
                    failure_reason: str = "",
                    interaction_id: str = "") -> None:
    import time
    entry = {
        "claim": claim[:200], "verdict": verdict,
        "score": score, "topic": topic,
        "correct": correct, "failure_reason": failure_reason,
        "interaction_id": interaction_id or "",
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


# ── Live calibration loop (real feedback, not the offline CSV) ──────
# The compute_ece/compute_calibration_map helpers above were written for an
# offline CSV eval that nothing in the running service produces. These read the
# ACTUAL feedback the /feedback endpoint records — the durable JSONL log (source
# of truth across restarts), falling back to the in-memory store — so the ECE /
# isotonic curve reflects real user corrections. Exposed via /metrics/calibration.

def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().upper() in ("YES", "TRUE", "1")


def load_feedback_records() -> list:
    """Return all feedback records: the durable JSONL log if present, else the
    in-memory store. Each record has score/correct/topic keys."""
    records: list = []
    try:
        if FEEDBACK_FILE.exists():
            with open(FEEDBACK_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        print(f"[FEEDBACK] read failed, using in-memory store: {e}")
    if not records:
        records = list(_feedback_store)
    return records


def compute_ece_from_records(records: list, n_bins: int = 10) -> dict:
    """Expected Calibration Error over feedback records (score 0-100, correct)."""
    bins = [[] for _ in range(n_bins)]
    total = 0
    for r in records:
        try:
            score = int(r.get("score", 50)) / 100.0
            score = min(max(score, 0.0), 1.0)
            correct = _to_bool(r.get("correct", False))
            bin_idx = min(int(score * n_bins), n_bins - 1)
            bins[bin_idx].append((score, correct))
            total += 1
        except (ValueError, TypeError):
            pass
    if total == 0:
        return {"ece": None, "total": 0, "bins": [], "verdict": "no-data"}
    ece = 0.0
    bin_stats = []
    for b in bins:
        if not b:
            continue
        avg_conf = sum(s for s, _ in b) / len(b)
        avg_acc  = sum(1 for _, c in b if c) / len(b)
        ece += (len(b) / total) * abs(avg_conf - avg_acc)
        bin_stats.append({"n": len(b), "conf": round(avg_conf, 3),
                          "acc": round(avg_acc, 3)})
    return {
        "ece": round(ece, 4),
        "total": total,
        "bins": bin_stats,
        "verdict": ("well-calibrated" if ece < 0.05
                    else "slightly overconfident" if ece < 0.10
                    else "overconfident"),
    }


def compute_calibration_map_from_records(records: list) -> dict:
    """Empirical accuracy per 10-point score bucket — the isotonic-style map
    from predicted score → observed correctness rate."""
    from collections import defaultdict
    bins = defaultdict(list)
    for r in records:
        try:
            score = int(r.get("score", 50))
            bins[(min(max(score, 0), 100) // 10) * 10].append(_to_bool(r.get("correct", False)))
        except (ValueError, TypeError):
            pass
    return {bucket: round(sum(res) / len(res) * 100)
            for bucket, res in sorted(bins.items()) if res}


def calibration_report(min_samples: int = 1) -> dict:
    """Full calibration snapshot from real feedback for /metrics/calibration."""
    records = load_feedback_records()
    n = len(records)
    ece = compute_ece_from_records(records)
    cal_map = compute_calibration_map_from_records(records)
    from collections import defaultdict
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for r in records:
        t = r.get("topic", "general") or "general"
        stats[t]["total"] += 1
        if _to_bool(r.get("correct", False)):
            stats[t]["correct"] += 1
    weak = sorted(
        [{"topic": t, "accuracy": round(s["correct"] / s["total"], 3), "n": s["total"]}
         for t, s in stats.items() if s["total"] >= 5],
        key=lambda x: x["accuracy"])
    return {
        "samples": n,
        "enough_data": n >= max(min_samples, 30),
        "ece": ece,
        "calibration_map": cal_map,
        "weak_domains": weak,
    }