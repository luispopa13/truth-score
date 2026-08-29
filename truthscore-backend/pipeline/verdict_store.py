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
import re
import json
import asyncio
import secrets
from pathlib import Path
from collections import OrderedDict, deque
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


async def save_verdict(payload: dict, user_id: str = "") -> str | None:
    """Persist a full verdict payload and return its short id (None on failure).

    `payload` is the VerifyResponse (or TextAnalysisResponse) as a plain dict —
    the caller passes model.model_dump(). We store it verbatim so /v/{id} can
    re-render the exact snapshot, plus a few denormalized fields for cheap OG
    metadata without re-parsing the whole blob. `user_id` (when the check was
    made while signed in) links the verdict to the user's private history.
    """
    _try_init_mongo()
    vid = new_verdict_id()
    record = {
        "id": vid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id or "",
        "claim": (payload.get("claim") or payload.get("text") or "")[:1000],
        "verdict": payload.get("verdict", "UNCERTAIN"),
        "score": payload.get("score", 50),
        "confidence": payload.get("confidence", ""),
        "topic": payload.get("topic", "general"),
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


def _scan_jsonl_user(user_id: str, limit: int) -> list[dict]:
    """Blocking JSONL scan for a user's verdicts. Newest last-write wins, then
    we return the most-recent `limit`. Used only when Mongo is unavailable."""
    if not VERDICTS_FILE.exists() or not user_id:
        return []
    by_id: "OrderedDict[str, dict]" = OrderedDict()
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
                if rec.get("user_id") == user_id:
                    # Last write wins (re-verify overwrites the same id).
                    by_id[rec["id"]] = rec
                    by_id.move_to_end(rec["id"])
    except Exception as e:
        print(f"[VERDICT-STORE] JSONL user scan failed: {e}")
        return []
    # File is append-order == chronological; take the newest `limit`, newest first.
    recs = list(by_id.values())
    recs.reverse()
    return recs[:limit]


def _summarize(rec: dict) -> dict:
    """Compact history-row projection (no heavy payload)."""
    return {
        "id": rec.get("id", ""),
        "created_at": rec.get("created_at", ""),
        "claim": rec.get("claim", ""),
        "verdict": rec.get("verdict", "UNCERTAIN"),
        "score": rec.get("score", 50),
        "confidence": rec.get("confidence", ""),
        "topic": rec.get("topic", "general"),
        "url": f"/v/{rec.get('id','')}",
    }


async def list_user_verdicts(user_id: str, limit: int = 50) -> list[dict]:
    """Return a user's most-recent verdicts as compact history rows (newest
    first). Mongo is primary (indexed-ish query, bounded); JSONL scan is the
    fallback. Never raises — a failure returns an empty history."""
    if not user_id:
        return []
    limit = max(1, min(int(limit or 50), 200))

    _try_init_mongo()
    if _mongo_collection is not None:
        try:
            cursor = _mongo_collection.find({"user_id": user_id}).sort(
                "created_at", -1
            ).limit(limit)
            docs = await cursor.to_list(length=limit)
            if docs is not None:
                return [_summarize(d) for d in docs]
        except Exception as e:
            print(f"[VERDICT-STORE] Mongo user query failed, JSONL fallback ({e})")

    recs = await asyncio.get_event_loop().run_in_executor(
        None, _scan_jsonl_user, user_id, limit
    )
    return [_summarize(r) for r in recs]


# ── Compounding knowledge base: "has this been checked before?" ──────
# The network-effect moat. Every saved verdict enriches a shared, public,
# cross-lingual fact archive; a new claim is matched against it so we can answer
# "TruthScore already checked this" instantly with a permalink. ChatGPT starts
# every conversation from zero — it cannot accumulate a shared verdict base.
#
# Matching is lexical (token-overlap / Jaccard) so it needs no ML model, works
# in any language, and is fully deterministic/testable. It is intentionally
# conservative: better to miss a loose match than to wrongly tell a user their
# claim was "already checked" by an unrelated verdict.
_TOKEN_RE = re.compile(r"[0-9a-zà-öø-ÿ]+", re.IGNORECASE)
# A tiny, multilingual-ish stopword set (EN + common RO) so shared filler words
# don't inflate similarity. Kept small on purpose — content words carry the signal.
_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "and", "or", "for", "that", "this", "these", "those",
    "it", "its", "as", "by", "with", "from", "has", "have", "had", "do", "does",
    "not", "no", "but", "if", "then", "than", "so", "who", "what", "when", "which",
    "si", "sau", "de", "la", "in", "un", "o", "este", "sunt", "era", "au", "cu",
    "ca", "ce", "care", "pe", "din", "nu", "se", "el", "ea", "le", "lui", "sa",
}
_RELATED_SCAN_MAX = int(os.getenv("RELATED_SCAN_MAX", "3000"))


def _tokenize(text: str) -> set:
    """Lowercased content-word token set (len>2, stopwords dropped)."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower())
            if len(t) > 2 and t not in _STOP}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def _scan_recent(limit_scan: int) -> list[dict]:
    """Return the most-recent `limit_scan` verdict records from the JSONL,
    deduped by id (last write wins). Bounded memory via a deque tail-window."""
    if not VERDICTS_FILE.exists():
        return []
    tail: "deque[str]" = deque(maxlen=max(1, limit_scan))
    try:
        with open(VERDICTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    tail.append(line)
    except Exception as e:
        print(f"[VERDICT-STORE] recent scan failed: {e}")
        return []
    by_id: "OrderedDict[str, dict]" = OrderedDict()
    for line in tail:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = rec.get("id")
        if rid:
            by_id[rid] = rec
            by_id.move_to_end(rid)
    return list(by_id.values())


async def find_similar_verdicts(claim: str, limit: int = 3,
                                min_sim: float = 0.5,
                                exclude_id: str = "") -> list[dict]:
    """Return prior verdicts whose claim overlaps `claim` above `min_sim`,
    most-similar first (each a compact row + `similarity` + /v/{id} permalink).
    Never raises — returns [] on any problem or empty query."""
    q = _tokenize(claim)
    if not q:
        return []
    limit = max(1, min(int(limit or 3), 10))
    min_sim = min(max(float(min_sim), 0.0), 1.0)

    candidates: list[dict] = []
    _try_init_mongo()
    if _mongo_collection is not None:
        try:
            # Exclude the heavy payload; we only need the denormalized fields.
            cursor = _mongo_collection.find({}, {"payload": 0}).sort(
                "created_at", -1).limit(_RELATED_SCAN_MAX)
            candidates = await cursor.to_list(length=_RELATED_SCAN_MAX)
        except Exception as e:
            print(f"[VERDICT-STORE] Mongo related scan failed, JSONL fallback ({e})")
            candidates = []
    if not candidates:
        candidates = await asyncio.get_event_loop().run_in_executor(
            None, _scan_recent, _RELATED_SCAN_MAX)

    scored: list[tuple[float, dict]] = []
    for rec in candidates:
        if exclude_id and rec.get("id") == exclude_id:
            continue
        sim = _jaccard(q, _tokenize(rec.get("claim", "")))
        if sim >= min_sim:
            scored.append((sim, rec))
    scored.sort(key=lambda x: x[0], reverse=True)

    out: list[dict] = []
    seen_claims: set = set()
    for sim, rec in scored:
        key = (rec.get("claim", "") or "").lower().strip()
        if key in seen_claims:
            continue
        seen_claims.add(key)
        row = _summarize(rec)
        row["similarity"] = round(sim, 3)
        out.append(row)
        if len(out) >= limit:
            break
    return out


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
