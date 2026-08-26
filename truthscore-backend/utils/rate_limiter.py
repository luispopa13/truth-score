"""
TruthScore -- Redis-backed rate limiter (token-bucket per user/plan).
Provides per-plan daily limits with atomic increment + TTL reset at midnight UTC.
"""
import os, time, logging
from typing import Tuple
from datetime import datetime, timezone

logger = logging.getLogger("truthscore.rate_limiter")

# Daily limits per plan — these are the REAL production limits.
# MUST stay in sync with auth.py's _PLAN_DAILY.  Source of truth: .env.
PLAN_LIMITS = {
    "free":       int(os.getenv("PLAN_FREE_DAILY", "10")),
    "pro":        int(os.getenv("PLAN_PRO_DAILY", "200")),
    "business":   int(os.getenv("PLAN_BUSINESS_DAILY", "800")),
    "enterprise": int(os.getenv("PLAN_ENTERPRISE_DAILY", "9999")),
}

_PLAN_FEATURES = {
    "free":       {"api_keys": 1, "batch": False, "pdf": False, "widget": False, "models": ["gemini"]},
    "pro":        {"api_keys": 5, "batch": True,  "pdf": True,  "widget": True,  "models": ["gemini", "groq"]},
    "business":   {"api_keys": 20, "batch": True,  "pdf": True,  "widget": True,  "models": ["gemini", "groq", "openai"]},
    "enterprise": {"api_keys": 100, "batch": True,  "pdf": True,  "widget": True,  "models": ["gemini", "groq", "openai"]},
}


def _utc_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _redis_key(user_id: str) -> str:
    return f"ts:rl:{user_id}:{_utc_date_str()}"


async def check_rate_limit(user: dict) -> Tuple[bool, dict]:
    """
    Atomically increment the daily counter for *user_id*.
    Returns (allowed, info_dict).
    """
    from utils.redis_client import get_async_redis

    if not user:
        # Anonymous users get a very small free quota
        redis = get_async_redis()
        if redis:
            info = await _check_anon(redis)
            return info["allowed"], info
        return False, {"allowed": False, "used": 0, "limit": 0, "plan": "anonymous",
                       "reset_in_hours": 24, "reason": "Redis not available — anonymous access disabled"}

    user_id = str(user.get("id") or user.get("_id") or "")
    plan = user.get("plan", "free")
    limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])

    redis = get_async_redis()
    if redis:
        try:
            key = _redis_key(user_id)
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, 86400)  # expire at end of day
            results = await pipe.execute()
            used = results[0]

            allowed = used <= limit
            info = {
                "allowed": allowed,
                "used": used,
                "limit": limit,
                "plan": plan,
                "reset_in_hours": _hours_until_midnight_utc(),
                "features": _PLAN_FEATURES.get(plan, _PLAN_FEATURES["free"]),
            }
            if not allowed:
                logger.warning("Rate limit exceeded: user=%s plan=%s used=%d limit=%d",
                               user_id, plan, used, limit)
            return allowed, info
        except Exception as e:
            logger.warning("Redis rate-limit error: %s — falling back to local", e)

    # Fallback: just check MongoDB usage record
    return await _check_mongo_usage(user)


async def _check_anon(redis) -> dict:
    """Anonymous users share a tiny global quota."""
    key = "ts:rl:anon:" + _utc_date_str()
    try:
        used = await redis.incr(key)
        await redis.expire(key, 86400)
        allowed = used <= 5
        return {"allowed": allowed, "used": used, "limit": 5, "plan": "anonymous",
                "reset_in_hours": _hours_until_midnight_utc()}
    except Exception:
        return {"allowed": False, "used": 0, "limit": 0, "plan": "anonymous",
                "reset_in_hours": 24, "reason": "Redis unavailable"}


async def _check_mongo_usage(user: dict) -> Tuple[bool, dict]:
    """Fallback when Redis is down: check MongoDB usage."""
    try:
        from auth import get_db
        db = get_db()
        user_id = str(user.get("id"))
        today = _utc_date_str()
        usage = await db.users.find_one(
            {"_id": __import__("bson").ObjectId(user_id)},
            {"usage": 1},
        )
        used = (usage or {}).get("usage", {}).get(today, 0)
        plan = user.get("plan", "free")
        limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        allowed = used < limit
        await db.users.update_one(
            {"_id": __import__("bson").ObjectId(user_id)},
            {"$inc": {f"usage.{today}": 1}},
        )
        return allowed, {"allowed": allowed, "used": used, "limit": limit,
                         "plan": plan, "reset_in_hours": _hours_until_midnight_utc()}
    except Exception:
        # Last resort: allow but log
        return True, {"allowed": True, "used": 0, "limit": 999, "plan": "free",
                      "note": "Rate-limit fallback — Redis and Mongo unavailable"}


def _hours_until_midnight_utc() -> int:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds() // 3600)


def get_plan_features(plan: str) -> dict:
    return _PLAN_FEATURES.get(plan, _PLAN_FEATURES["free"])
