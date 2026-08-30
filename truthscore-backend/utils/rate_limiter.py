"""
TruthScore -- Redis-backed rate limiter (token-bucket per user/plan).
Provides per-plan daily limits with atomic increment + TTL reset at midnight UTC.
"""
import os, time, logging
from typing import Tuple
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("truthscore.rate_limiter")

# Daily limits AND feature matrix per plan — the single source of truth lives
# in auth.py; imported here so the two never drift. auth.py has no top-level
# import of this module, so importing auth here is cycle-free. We import the
# effective-limit FUNCTION (base plan + trial + referral bonus) rather than the
# raw plan table, so trial/referral bonuses are enforced identically on this
# Redis path and on auth's own Mongo fallback. _PLAN_FEATURES is imported (not
# copied) so the `features` dict is identical on both code paths below.
from auth import get_effective_daily_limit
from auth import _PLAN_FEATURES
# Re-exported for consumers that display the raw base-plan daily table
# (e.g. main.py's plan/config endpoint). Still sourced from auth — no second
# copy. Enforcement uses get_effective_daily_limit(), not this table.
from auth import _PLAN_DAILY as PLAN_LIMITS


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
    limit = get_effective_daily_limit(user)

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
    from utils.abuse import ANON_DAILY_CAP
    key = "ts:rl:anon:" + _utc_date_str()
    try:
        used = await redis.incr(key)
        await redis.expire(key, 86400)
        # `used` is POST-increment: allow exactly ANON_DAILY_CAP, block the next.
        allowed = used <= ANON_DAILY_CAP
        return {"allowed": allowed, "used": used, "limit": ANON_DAILY_CAP, "plan": "anonymous",
                "reset_in_hours": _hours_until_midnight_utc()}
    except Exception:
        return {"allowed": False, "used": 0, "limit": 0, "plan": "anonymous",
                "reset_in_hours": 24, "reason": "Redis unavailable"}


async def _check_mongo_usage(user: dict) -> Tuple[bool, dict]:
    """Fallback when Redis is down: check MongoDB usage."""
    try:
        from auth import get_db
        db = get_db()
        user_id = str(user.get("id") or user.get("_id") or "")
        if not user_id:
            raise ValueError("Cannot resolve user_id for rate check")
        today = _utc_date_str()
        usage = await db.users.find_one(
            {"_id": __import__("bson").ObjectId(user_id)},
            {"usage": 1},
        )
        used = (usage or {}).get("usage", {}).get(today, 0)
        plan = user.get("plan", "free")
        limit = get_effective_daily_limit(user)
        # `used` is PRE-increment: `used < limit` allows exactly `limit` requests/day.
        allowed = used < limit
        if allowed:
            await db.users.update_one(
                {"_id": __import__("bson").ObjectId(user_id)},
                {"$inc": {f"usage.{today}": 1}},
            )
        new_used = used + 1 if allowed else used
        return allowed, {"allowed": allowed, "used": new_used, "limit": limit,
                         "plan": plan, "reset_in_hours": _hours_until_midnight_utc()}
    except Exception:
        # Both Redis and Mongo are unreachable. Fail CLOSED for the free tier —
        # otherwise an outage becomes an unlimited-free-verifications hole and a
        # direct cost/DoS attack (just knock the stores over). Paid users are
        # trusted through the blip: they've paid, outages are rare, and we don't
        # punish customers for our own infra failing.
        plan = user.get("plan", "free") or "free"
        if plan == "free":
            return False, {"allowed": False, "used": 0, "limit": 0, "plan": plan,
                           "reset_in_hours": 24,
                           "note": "Rate-limit stores unavailable — free tier paused"}
        return True, {"allowed": True, "used": 0,
                      "limit": get_effective_daily_limit(user), "plan": plan,
                      "note": "Rate-limit fallback — stores unavailable, paid user allowed"}


def _hours_until_midnight_utc() -> int:
    now = datetime.now(timezone.utc)
    # Next UTC midnight (start of tomorrow); today's midnight is already past.
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((next_midnight - now).total_seconds() // 3600))


def get_plan_features(plan: str) -> dict:
    return _PLAN_FEATURES.get(plan, _PLAN_FEATURES["free"])
