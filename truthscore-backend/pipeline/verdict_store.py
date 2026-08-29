"""
TruthScore -- Permanent Shareable Verdict Store
================================================
Persists the FULL verdict payload of a verification under a short, URL-safe id
so it can be served forever at a stable permalink (GET /v/{id}) — a frozen,
crawlable, citable snapshot of "what TruthScore concluded, and on what evidence."

Why this is the moat: ChatGPT & co. give an ephemeral answer inside a private
chat. A TruthScore verdict gets a permanent public URL with the sources baked
in, an Open-Graph card that unfurls in chats/social, and a snapshot that never
changes even if the live model later would. That link is shareable, embeddable,
and indexable — every share is a citation back to the product.

Storage mirrors pipeline/case_study.py conventions:
  • Append-only JSONL is the local source of truth (crash-safe, zero infra).
  • Optional MongoDB mirror (same MONGODB_URL/MONGODB_DB env) is the durable
    copy on cloud hosts where the local FS is wiped on redeploy.
  • An in-memory LRU caches the most-recent lookups so hot permalinks don't hit
    disk/Mongo on every crawl.

This module never raises to its callers: saving is best-effort (a failed save
just means no permalink for that check), and loading returns None on any miss.
"""
import os
import json
import asyncio
import secrets
from pathlib import Path
from collections import OrderedDict
from datetime import datetime, timezone

_BACKEND_ROOT = Path(__file__).parent.parent
_STORE_DIR = _BACKEND_ROOT / "data" / "verdicts"
_STORE_DIR.mkdir(parents=True, exist_ok=True)

VERDICTS_FILE = _STORE_DIR / "verdicts.jsonl"

_write_lock = asyncio.Lock()

# Short id: 9 url-safe chars from 6 random bytes (~2.8e14 space). Collisions are
# astronomically unlikely at our volume; Mongo's _id uniqueness would catch one
# anyway (we simply regenerate on the rare insert clash).
_ID_BYTES = 6

# In-memory LRU for hot permalinks (crawlers hammer the same id on unfurl).
_CACHE_MAX = int(os.getenv("VERDICT_CACHE_MAX", "512"))
_cache: "OrderedDict[str, dict]" = OrderedDict()

_mongo_collection = None
_mongo_init_tried = False


def _try_init_mongo():
    global _mongo_collection, _mongo_init_tried
    if _mongo_init_tried:
        return
    _mongo_init_tried = True
    mongo_url = os.getenv("MONGODB_URL", "")
    db_name = os.getenv("MONGODB_DB", "truthscore")
    if not mongo_url:
        return
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=2000)
        _mongo_collection = client[db_name]["verdicts"]
        print(f"[VERDICT-STORE] MongoDB mirror enabled (db={db_name})")
    except Exception as e:
        print(f"[VERDICT-STORE] MongoDB mirror disabled ({e}) -- using JSONL only")


def new_verdict_id() -> str:
    return secrets.token_urlsafe(_ID_BYTES)


def _cache_put(vid: str, record: dict) -> None:
    _cache[vid] = record
    _cache.move_to_end(vid)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


async def save_verdict(payload: dict) -> str | None:
    """Persist a full verdict payload and return its short id (None on failure).

    `payload` is the VerifyResponse (or TextAnalysisResponse) as a plain dict —
    the caller passes model.model_dump(). We store it verbatim so /v/{id} can
    re-render the exact snapshot, plus a few denormalized fields for cheap OG
    metadata without re-parsing the whole blob.
    """
    _try_init_mongo()
    vid = new_verdict_id()
    record = {
        "id": vid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claim": (payload.get("claim") or payload.get("text") or "")[:1000],
        "verdict": payload.get("verdict", "UNCERTAIN"),
        "score": payload.get("score", 50),
        "confidence": payload.get("confidence", ""),
        "payload": payload,
    }

    line = json.dumps(record, ensure_ascii=False, default=str)
    async with _write_lock:
        try:
            with open(VERDICTS_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            print(f"[VERDICT-STORE] JSONL write failed: {e}")
            # If we can't persist locally AND have no Mongo, the permalink would
            # 404 — signal failure so the caller omits the share id.
            if _mongo_collection is None:
                return None

    if _mongo_collection is not None:
        try:
            doc = dict(record)
            doc["_id"] = vid
            await _mongo_collection.insert_one(doc)
        except Exception as e:
            print(f"[VERDICT-STORE] Mongo insert failed (non-fatal): {e}")

    _cache_put(vid, record)
    return vid


def _scan_jsonl(vid: str) -> dict | None:
    """Linear scan of the JSONL for a given id. Last write wins (re-verify)."""
    if not VERDICTS_FILE.exists():
        return None
    found = None
    try:
        with open(VERDICTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") == vid:
                    found = rec
    except Exception as e:
        print(f"[VERDICT-STORE] JSONL read failed: {e}")
    return found


async def load_verdict(vid: str) -> dict | None:
    """Return the stored record for `vid`, or None. Cache -> Mongo -> JSONL."""
    if not vid:
        return None
    hit = _cache.get(vid)
    if hit is not None:
        _cache.move_to_end(vid)
        return hit

    _try_init_mongo()
    if _mongo_collection is not None:
        try:
            doc = await _mongo_collection.find_one({"_id": vid})
            if doc:
                doc.pop("_id", None)
                _cache_put(vid, doc)
                return doc
        except Exception as e:
            print(f"[VERDICT-STORE] Mongo read failed, falling back to JSONL ({e})")

    # JSONL scan is blocking; punt to a thread so a big log doesn't stall the loop.
    rec = await asyncio.get_event_loop().run_in_executor(None, _scan_jsonl, vid)
    if rec is not None:
        _cache_put(vid, rec)
    return rec
