"""
TruthScore -- Evidence Cache (search-level caching).

Caches RAW SEARCH RESULTS by query so that repeated or near-duplicate
searches (same claim re-checked, similar claims, batch runs) never hit
paid search APIs again. This is the single most important cost lever:
Tavily costs $0.008/credit ($0.016 for advanced depth), and the pipeline
fires 2-3 searches per verification.

Also enforces a GLOBAL DAILY BUDGET on paid searches via Redis, with
graceful degradation to free-only sources when the budget is exhausted.
"""
import os, json, logging, hashlib
from typing import Optional

logger = logging.getLogger("truthscore.evidence_cache")

EVIDENCE_TTL        = int(os.getenv("EVIDENCE_CACHE_TTL", str(86400)))       # 24h default
PAID_SEARCH_DAILY_CAP = int(os.getenv("PAID_SEARCH_DAILY_CAP", "3000"))     # global cap/day
_search_cache_local = {}   # in-process fallback {query_hash: results}


def _query_key(query: str, source: str) -> str:
    h = hashlib.sha256(f"{source}:{query.strip().lower()}".encode()).hexdigest()[:24]
    return f"ts:ev:{h}"


async def get_cached_evidence(query: str, source: str) -> Optional[list]:
    """Return cached raw-source dicts for this query, or None."""
    key = _query_key(query, source)

    # In-process fast path
    if key in _search_cache_local:
        return _search_cache_local[key]

    try:
        from utils.redis_client import get_async_redis
        redis = get_async_redis()
        if redis:
            raw = await redis.get(key)
            if raw:
                data = json.loads(raw)
                _search_cache_local[key] = data
                logger.debug("Evidence cache HIT (%s): %s", source, query[:50])
                return data
    except Exception as e:
        logger.debug("Evidence cache redis error: %s", e)

    # diskcache fallback
    try:
        from utils.cache import cache as disk
        data = disk.get("ev:" + key)
        if data is not None:
            _search_cache_local[key] = data
            return data
    except Exception:
        pass
    return None


async def store_cached_evidence(query: str, source: str, sources_as_dicts: list):
    key = _query_key(query, source)
    payload = [s.model_dump() if hasattr(s, "model_dump") else s for s in sources_as_dicts]
    _search_cache_local[key] = payload

    try:
        from utils.redis_client import get_async_redis
        redis = get_async_redis()
        if redis:
            await redis.setex(key, EVIDENCE_TTL, json.dumps(payload, ensure_ascii=False))
            return
    except Exception as e:
        logger.debug("Evidence store redis error: %s", e)

    try:
        from utils.cache import cache as disk
        disk.set("ev:" + key, payload, expire=EVIDENCE_TTL)
    except Exception:
        pass


# ── Paid-search budget guard ────────────────────────────────────

async def paid_search_allowed(source: str = "tavily") -> bool:
    """
    Global daily cap on PAID searches across all users & instances.
    Protects you from a runaway loop draining your Tavily account.
    Returns True if the search may proceed (and increments the counter).
    """
    try:
        from utils.redis_client import get_async_redis
        redis = get_async_redis()
        if not redis:
            return True   # no Redis -> no enforcement; local dev mode
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"ts:budget:{source}:{day}"
        used = await redis.incr(key)
        await redis.expire(key, 172800)  # keep 2 days
        if used > PAID_SEARCH_DAILY_CAP:
            logger.warning("Paid-search budget EXHAUSTED for %s (%d/%d)",
                           source, used - 1, PAID_SEARCH_DAILY_CAP)
            return False
        return True
    except Exception:
        return True


async def paid_search_budget_status() -> dict:
    try:
        from utils.redis_client import get_async_redis
        redis = get_async_redis()
        if not redis:
            return {"cap": PAID_SEARCH_DAILY_CAP, "used_today": None,
                    "note": "Redis unavailable — budget not enforced"}
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        used = int(await redis.get(f"ts:budget:tavily:{day}") or 0)
        return {"cap": PAID_SEARCH_DAILY_CAP, "used_today": used}
    except Exception as e:
        return {"cap": PAID_SEARCH_DAILY_CAP, "error": str(e)}