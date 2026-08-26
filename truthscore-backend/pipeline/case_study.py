"""
TruthScore -- User Case Study Logger
======================================
Self-contained module that logs every /verify interaction and every
piece of user feedback to local JSONL files (plain, append-only, never
corrupted, trivially loadable with pandas for thesis analysis), with
an OPTIONAL best-effort mirror to MongoDB if MONGO_URI is configured.

Why JSONL as the source of truth:
  - Zero extra infrastructure -- works even if MongoDB is down.
  - Append-only -- a crash mid-write can never corrupt previous rows.
  - One row = one JSON object = trivially loadable:
        import pandas as pd
        df = pd.read_json("data/case_study/interactions.jsonl", lines=True)

This module does NOT touch verify.py or models.py -- it is called
exclusively from main.py around the existing /verify and /feedback
routes, so it cannot break any existing pipeline logic.
"""
import os
import csv
import json
import uuid
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from statistics import mean, median

# ── Storage location (absolute path -- independent of cwd) ──────
_BACKEND_ROOT = Path(__file__).parent.parent
_DATA_DIR     = _BACKEND_ROOT / "data" / "case_study"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

INTERACTIONS_FILE = _DATA_DIR / "interactions.jsonl"
FEEDBACK_FILE      = _DATA_DIR / "feedback.jsonl"

_write_lock_i = asyncio.Lock()
_write_lock_f = asyncio.Lock()

# ── Optional MongoDB mirror (best-effort, never blocks/raises) ──
_mongo_collection_i = None
_mongo_collection_f = None
_mongo_init_tried    = False


def _try_init_mongo():
    """
    Best-effort MongoDB setup. Silently does nothing if unavailable.

    Uses the SAME env vars as auth.py (MONGODB_URL / MONGODB_DB) so a
    single MongoDB connection string covers both user accounts and the
    case-study mirror -- important on cloud hosting, where the local
    JSONL files live on an ephemeral filesystem and are wiped on every
    redeploy/restart. MongoDB is the durable copy there.
    """
    global _mongo_collection_i, _mongo_collection_f, _mongo_init_tried
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
        _mongo_collection_i = db["case_study_interactions"]
        _mongo_collection_f = db["case_study_feedback"]
        print(f"[CASE-STUDY] MongoDB mirror enabled (db={db_name})")
    except Exception as e:
        print(f"[CASE-STUDY] MongoDB mirror disabled ({e}) -- using JSONL only")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public API ────────────────────────────────────────────────

async def log_interaction(data: dict) -> str:
    """
    Logs one /verify interaction. Returns a unique interaction_id
    that the caller can hand back to the client (e.g. via a response
    header) so subsequent feedback can be linked precisely.

    Expected (all optional) keys in `data`:
        claim, language, topic, verdict, score, confidence,
        evidence_count, supporting_count, contradicting_count,
        neutral_count, models_used, cached, duration_ms,
        user_id, user_plan, source ("dashboard" | "extension" | "api")
    """
    _try_init_mongo()
    interaction_id = str(uuid.uuid4())
    record = {
        "interaction_id": interaction_id,
        "timestamp": _now_iso(),
        **data,
    }

    line = json.dumps(record, ensure_ascii=False, default=str)
    async with _write_lock_i:
        try:
            with open(INTERACTIONS_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            print(f"[CASE-STUDY] Failed to write interaction log: {e}")

    if _mongo_collection_i is not None:
        try:
            await _mongo_collection_i.insert_one(dict(record))
        except Exception as e:
            print(f"[CASE-STUDY] Mongo insert failed (non-fatal): {e}")

    return interaction_id


async def log_feedback(interaction_id: str, correct: bool, comment: str = None, user_id: str = None) -> None:
    """Logs one piece of user feedback, linked by interaction_id."""
    _try_init_mongo()
    record = {
        "interaction_id": interaction_id,
        "timestamp": _now_iso(),
        "correct": correct,
        "comment": comment,
        "user_id": user_id,
    }

    line = json.dumps(record, ensure_ascii=False, default=str)
    async with _write_lock_f:
        try:
            with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            print(f"[CASE-STUDY] Failed to write feedback log: {e}")

    if _mongo_collection_f is not None:
        try:
            await _mongo_collection_f.insert_one(dict(record))
        except Exception as e:
            print(f"[CASE-STUDY] Mongo insert failed (non-fatal): {e}")


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


async def _fetch_from_mongo() -> tuple:
    """
    Pulls all interactions + feedback from MongoDB. Used as the durable
    source on cloud hosting, where the local JSONL files are wiped on
    every redeploy/restart (ephemeral filesystem).
    """
    _try_init_mongo()
    if _mongo_collection_i is None:
        return None, None
    try:
        interactions = await _mongo_collection_i.find({}, {"_id": 0}).to_list(length=None)
        feedback     = await _mongo_collection_f.find({}, {"_id": 0}).to_list(length=None)
        return interactions, feedback
    except Exception as e:
        print(f"[CASE-STUDY] Mongo read failed, falling back to JSONL ({e})")
        return None, None


async def get_stats() -> dict:
    """
    Quick summary statistics for monitoring data collection progress.
    Reads from MongoDB if configured (durable across redeploys),
    otherwise from the local JSONL files.
    """
    interactions, feedback = await _fetch_from_mongo()
    if interactions is None:
        interactions = _read_jsonl(INTERACTIONS_FILE)
        feedback     = _read_jsonl(FEEDBACK_FILE)

    if not interactions:
        return {"total_interactions": 0, "total_feedback": 0}

    fb_by_id = {f["interaction_id"]: f for f in feedback if "interaction_id" in f}

    durations  = [r["duration_ms"] for r in interactions if isinstance(r.get("duration_ms"), (int, float))]
    verdicts   = {}
    topics     = {}
    confidences = {}
    cached_count = 0

    for r in interactions:
        v = r.get("verdict", "UNKNOWN")
        verdicts[v] = verdicts.get(v, 0) + 1
        t = r.get("topic", "unknown")
        topics[t] = topics.get(t, 0) + 1
        c = r.get("confidence", "UNKNOWN")
        confidences[c] = confidences.get(c, 0) + 1
        if r.get("cached"):
            cached_count += 1

    agree = sum(1 for f in feedback if f.get("correct") is True)
    disagree = sum(1 for f in feedback if f.get("correct") is False)

    return {
        "total_interactions": len(interactions),
        "total_feedback": len(feedback),
        "feedback_coverage_pct": round(100 * len(fb_by_id) / len(interactions), 1) if interactions else 0,
        "user_agreement_pct": round(100 * agree / (agree + disagree), 1) if (agree + disagree) else None,
        "user_agree_count": agree,
        "user_disagree_count": disagree,
        "cached_pct": round(100 * cached_count / len(interactions), 1),
        "verdict_distribution": verdicts,
        "topic_distribution": topics,
        "confidence_distribution": confidences,
        "duration_ms": {
            "mean": round(mean(durations), 1) if durations else None,
            "median": round(median(durations), 1) if durations else None,
            "min": round(min(durations), 1) if durations else None,
            "max": round(max(durations), 1) if durations else None,
            "n": len(durations),
        },
        "source": "mongodb" if os.getenv("MONGODB_URL") else "local_jsonl",
        "data_files": {
            "interactions": str(INTERACTIONS_FILE),
            "feedback": str(FEEDBACK_FILE),
        },
    }


async def export_merged_csv() -> str:
    """
    Merges interactions + feedback (left join on interaction_id) into a
    single flat CSV file, ready to open in Excel / load with pandas for
    the thesis evaluation chapter. Reads from MongoDB if configured
    (durable across redeploys), otherwise from local JSONL. Returns the
    output file path.
    """
    interactions, feedback = await _fetch_from_mongo()
    if interactions is None:
        interactions = _read_jsonl(INTERACTIONS_FILE)
        feedback     = _read_jsonl(FEEDBACK_FILE)

    fb_by_id = {}
    for f in (feedback or []):
        fb_by_id.setdefault(f.get("interaction_id"), f)  # first feedback wins

    out_path = _DATA_DIR / "export_merged.csv"

    if not interactions:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            f.write("no_data_yet\n")
        return str(out_path)

    fieldnames = []
    for r in interactions:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)
    fieldnames += ["feedback_correct", "feedback_comment", "feedback_timestamp"]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in interactions:
            row = dict(r)
            fb = fb_by_id.get(r.get("interaction_id"))
            if fb:
                row["feedback_correct"]   = fb.get("correct")
                row["feedback_comment"]   = fb.get("comment")
                row["feedback_timestamp"] = fb.get("timestamp")
            for k, v in list(row.items()):
                if isinstance(v, (list, dict)):
                    row[k] = json.dumps(v, ensure_ascii=False)
            writer.writerow(row)

    return str(out_path)