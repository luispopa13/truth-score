"""
TruthScore -- Verdict Cache (Redis-backed exact match + L1 in-process).

LATENCY DESIGN (why no vector similarity here):
  The earlier draft scanned every cached embedding and computed cosine
  similarity in Python — O(n) over the whole cache, seconds at scale.
  Removed. Exact normalized matching already catches the overwhelming
  majority of repeat claims (case/punctuation/diacritics-insensitive),
  and evidence_cache handles near-duplicate *searches*. This layer stays
  O(1): one dict probe -> one Redis GET (~1ms) -> diskcache fallback.

Cache hits are FREE: no LLM, no search, no rate-limit cost.
"""
import os, json, logging
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("truthscore.verdict_cache")

SEM_CACHE_TTL = int(os.getenv("SEM_CACHE_TTL", "86400"))
_L1_MAX = int(os.getenv("VERDICT_L1_SIZE", "512"))

# L1: hottest claims served with zero I/O
_l1: OrderedDict = OrderedDict()


def _l1_get(sig: str) -> Optional[dict]:
    hit = _l1.get(sig)
    if hit is not None:
        _l1.move_to_end(sig)
    return hit


def _l1_put(sig: str, data: dict):
    _l1[sig] = data
    _l1.move_to_end(sig)
    while len(_l1) > _L1_MAX:
        _l1.popitem(last=False)


def _claim_signature(text: str) -> str:
    """Deterministic key: case/punct/whitespace/diacritics-insensitive."""
    import re as _re, unicodedata as _ud
    t = text.lower().strip()
    t = _re.sub(r"\s+", " ", t)
    t = t.rstrip("?!.").strip()
    t = _ud.normalize("NFKD", t)
    t = "".join(c for c in t if not _ud.combining(c))
    return "v3:" + t[:200]


async def semantic_lookup(text: str, redis_cli=None) -> Optional[dict]:
    """Exact-match verdict lookup. Returns cached VerifyResponse dict or None."""
    sig = _claim_signature(text)

    # L1 (in-process, ~0µs)
    hit = _l1_get(sig)
    if hit is not None:
        out = dict(hit)
        out["cached"] = True
        return out

    # L2 Redis (~0.5-2ms)
    try:
        if redis_cli is None:
            from utils.redis_client import get_async_redis
            redis_cli = get_async_redis()
        if redis_cli:
            raw = await redis_cli.get(f"ts:cache:{sig}")
            if raw:
                data = json.loads(raw)
                data["cached"] = True
                _l1_put(sig, data)
                logger.info("Verdict cache: REDIS hit")
                return data
    except Exception as e:
        logger.debug("Redis verdict lookup error: %s", e)

    # L3 diskcache (local fallback / dev)
    try:
        from utils.cache import cache
        hit = cache.get(sig)
        if hit:
            hit["cached"] = True
            _l1_put(sig, hit)
            return hit
    except Exception:
        pass
    return None


async def semantic_store(text: str, result: dict, redis_cli=None) -> None:
    """Store a verified result in all cache layers."""
    sig = _claim_signature(text)
    _l1_put(sig, result)

    try:
        from utils.cache import cache
        cache.set(sig, result, expire=SEM_CACHE_TTL)
    except Exception:
        pass

    try:
        if redis_cli is None:
            from utils.redis_client import get_async_redis
            redis_cli = get_async_redis()
        if redis_cli:
            payload = json.dumps(result, ensure_ascii=False, default=str)
            await redis_cli.setex(f"ts:cache:{sig}", SEM_CACHE_TTL, payload)
    except Exception as e:
        logger.debug("Redis verdict store error: %s — kept local", e)
