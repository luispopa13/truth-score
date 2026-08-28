"""
TruthScore Authentication & User Management
MongoDB + JWT + Rate Limiting + Stripe Plans
"""
import os, time, hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv
load_dotenv(override=True)  # Ensure .env is loaded in auth.py too

# ── Fix bcrypt 5.0 incompatibility with passlib ────────────────
import types as _types
import bcrypt as _bcrypt_check
if not hasattr(_bcrypt_check, '__about__'):
    _about_mod = _types.ModuleType('__about__')
    _about_mod.__version__ = getattr(_bcrypt_check, '__version__', '4.0.0')
    _bcrypt_check.__about__ = _about_mod


from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
import warnings
_USE_PASSLIB = False
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from passlib.context import CryptContext
        _pwd_ctx_test = CryptContext(schemes=["bcrypt"], deprecated="auto")
        _pwd_ctx_test.hash("test123")  # Test it works
        _USE_PASSLIB = True
except Exception:
    _USE_PASSLIB = False

import bcrypt as _bcrypt_direct
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
import httpx

# ── Config ─────────────────────────────────────────────────────
SECRET_KEY   = os.getenv("JWT_SECRET", "CHANGE_THIS_SECRET_IN_PRODUCTION_32chars")
# ── JWT secret hardening (FAIL CLOSED) ─────────────────────────
# A publicly-known HS256 signing secret = full account/plan takeover (anyone
# can forge a token for any user). So we refuse to boot with the placeholder
# or a too-short secret BY DEFAULT, on every host. The only escape hatch is an
# explicit local-dev opt-in (DEV_INSECURE=1 or ENV=development) — you have to
# knowingly ask for the insecure secret; you can't get it by forgetting to set
# one on an un-labelled VM (the previous behaviour, which was the vuln).
_JWT_PLACEHOLDER = "CHANGE_THIS_SECRET_IN_PRODUCTION_32chars"
_DEV_OPT_IN = (os.getenv("DEV_INSECURE", "").lower() in ("1", "true", "yes")
               or os.getenv("ENV", "").lower() in ("development", "dev", "local"))
if SECRET_KEY == _JWT_PLACEHOLDER or len(SECRET_KEY) < 32:
    if not _DEV_OPT_IN:
        raise RuntimeError(
            "JWT_SECRET is unset/placeholder/too-short. Set JWT_SECRET to a "
            "random 32+ character secret. For local development only, set "
            "DEV_INSECURE=1 (or ENV=development) to bypass this check."
        )
    import sys as _sys
    print("[WARN] JWT_SECRET is placeholder or <32 chars — INSECURE dev mode "
          "(DEV_INSECURE opt-in). Never use this in production.", file=_sys.stderr)
ALGORITHM    = "HS256"
TOKEN_EXPIRE = 60 * 24 * 30  # 30 days in minutes

MONGO_URL    = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
DB_NAME      = os.getenv("MONGODB_DB", "truthscore")

# ── Plans (REAL limits — monetization engine) ──────────────────
# Daily limits are the actual cap per user.  Cost per claim ≈ $0.0006,
# so even if a user maxes out their free quota every day the cost is
# negligible compared to upgrade conversion rate.
_PLAN_DAILY = {
    "free":       int(os.getenv("PLAN_FREE_DAILY", "10")),
    "pro":        int(os.getenv("PLAN_PRO_DAILY", "200")),
    "business":   int(os.getenv("PLAN_BUSINESS_DAILY", "800")),
    "enterprise": int(os.getenv("PLAN_ENTERPRISE_DAILY", "9999")),
}

# After this many checks in a day, routing switches to eco mode (cheap
# models, no paid search) — protects margin from heavy-day users without
# any hard block. Free never reaches it. Business threshold lower than
# Pro because paragraphs let power users burn hundreds of fresh claims/day;
# at €29.99 the absolute-worst case must still land near break-even.
_ECO_AFTER = {
    "free":       999999,
    "pro":        int(os.getenv("PRO_ECO_AFTER", "100")),
    "business":   int(os.getenv("BUSINESS_ECO_AFTER", "350")),
    "enterprise": 999999,
}

_PLAN_PRICES = {
    "free":       0,
    "pro":        9.99,
    "business":   29.99,
    "enterprise": 199,
}

# Canonical per-plan feature matrix. This is the ONE definition — utils/
# rate_limiter.py imports it (never redefines) so the `features` dict a user
# sees is identical whether the rate-limit ran through Redis or the Mongo
# fallback. `api_keys` = max concurrent API keys; `models` is advisory (routing
# is decided by eco-mode + pick_model, not this list).
_PLAN_FEATURES = {
    "free":       {"batch": False, "pdf": False, "widget": False, "seats": 1,
                   "api_quota": 0,    "api_keys": 1,   "models": ["eco"]},
    "pro":        {"batch": True,  "pdf": True,  "widget": True,  "seats": 1,
                   "api_quota": 0,    "api_keys": 5,   "models": ["gemini", "groq"]},
    "business":   {"batch": True,  "pdf": True,  "widget": True,  "seats": 3,
                   "api_quota": 5000, "api_keys": 20,  "models": ["gemini", "groq", "gpt4o-mini"]},
    "enterprise": {"batch": True,  "pdf": True,  "widget": True,  "seats": 0,
                   "api_quota": -1,   "api_keys": 100, "models": ["all"]},   # seats 0 = custom
}


def _get_plans():
    return {
        "free": {
            "name": "Free", "price": _PLAN_PRICES["free"],
            "daily_limit": _PLAN_DAILY["free"],
            "batch_limit": 0, "pdf": False, "widget": False,
            "price_id": None,
            "features": _PLAN_FEATURES["free"],
            "ads": True,           # free tier is ad-supported on dashboard
            "bonus_via_feedback": True,
        },
        "pro": {
            "name": "Pro", "price": _PLAN_PRICES["pro"],
            "daily_limit": _PLAN_DAILY["pro"],
            "batch_limit": 50, "pdf": True, "widget": True,
            "price_id": os.getenv("STRIPE_PRO_PRICE_ID", ""),
            "features": _PLAN_FEATURES["pro"],
            "ads": False,
            "eco_after_daily": _ECO_AFTER["pro"],
        },
        "business": {
            "name": "Business", "price": _PLAN_PRICES["business"],
            "daily_limit": _PLAN_DAILY["business"],
            "batch_limit": 500, "pdf": True, "widget": True,
            # NO fallback to the enterprise price id — that would charge a
            # business customer the enterprise rate while granting business.
            # Empty => create_checkout raises a clear "set STRIPE_BUSINESS_PRICE_ID".
            "price_id": os.getenv("STRIPE_BUSINESS_PRICE_ID", ""),
            "features": _PLAN_FEATURES["business"],
            "ads": False,
            "eco_after_daily": _ECO_AFTER["business"],
        },
        "enterprise": {
            "name": "Enterprise", "price": _PLAN_PRICES["enterprise"],
            "daily_limit": _PLAN_DAILY["enterprise"],
            "batch_limit": 9999, "pdf": True, "widget": True,
            "price_id": os.getenv("STRIPE_ENT_PRICE_ID", ""),
            "features": _PLAN_FEATURES["enterprise"],
            "ads": False,
        },
    }


PLANS = _get_plans()

# ── Crypto ─────────────────────────────────────────────────────
if _USE_PASSLIB:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
security  = HTTPBearer(auto_error=False)

# ── MongoDB ────────────────────────────────────────────────────
_client: Optional[AsyncIOMotorClient] = None

# Connection-pool settings — tuned for 1000 concurrent users
_MONGO_POOL_SIZE = int(os.getenv("MONGO_POOL_SIZE", "100"))


def get_db():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(
            MONGO_URL,
            serverSelectionTimeoutMS=5000,
            maxPoolSize=_MONGO_POOL_SIZE,
            minPoolSize=10,
            maxIdleTimeMS=30000,
            retryWrites=True,
        )
    return _client[DB_NAME]

# ── Pydantic models ────────────────────────────────────────────
class UserRegister(BaseModel):
    email: str
    password: str
    name: str = ""
    turnstile_token: str = ""   # Cloudflare Turnstile widget response (optional dev)

class UserLogin(BaseModel):
    email: str
    password: str

class UserOut(BaseModel):
    id: str
    email: str
    name: str
    plan: str
    daily_limit: int
    used_today: int
    stripe_customer_id: str = ""

class GoogleAuthRequest(BaseModel):
    token: str

# ── Helpers ────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    pw_bytes = pw.encode('utf-8')[:72]  # bcrypt max 72 bytes
    if _USE_PASSLIB:
        try:
            return pwd_ctx.hash(pw)
        except Exception:
            pass
    # Direct bcrypt fallback
    salt = _bcrypt_direct.gensalt()
    return _bcrypt_direct.hashpw(pw_bytes, salt).decode('utf-8')

def verify_password(plain: str, hashed: str) -> bool:
    plain_bytes = plain.encode('utf-8')[:72]
    if _USE_PASSLIB:
        try:
            return pwd_ctx.verify(plain, hashed)
        except Exception:
            pass
    # Direct bcrypt fallback
    try:
        return _bcrypt_direct.checkpw(plain_bytes, hashed.encode('utf-8'))
    except Exception:
        return False

def create_token(user_id: str, token_version: int = 0) -> str:
    import uuid as _uuid
    expire = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRE)
    # `tv` (token version) pins the token to the user's current version. Bumping
    # user.token_version (logout-everywhere / password change) instantly voids
    # every token that carried the old value — enforced in get_current_user
    # straight off the Mongo doc, so it works even when Redis is down.
    payload = {"sub": user_id, "exp": expire, "jti": _uuid.uuid4().hex,
               "tv": int(token_version)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    """Returns full payload (sub + jti) — raises 401 on invalid/expired."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ── Auth endpoints helpers ─────────────────────────────────────
async def register_user(data: UserRegister, client_ip: str = "") -> dict:
    """Register a new account, with abuse checks (disposable email,
    Turnstile CAPTCHA, per-IP velocity)."""
    from utils.abuse import is_disposable_email, turnstile_verify, ip_can_register

    if is_disposable_email(data.email):
        raise HTTPException(status_code=400,
                            detail="Adresele de email temporare nu sunt permise")

    if not await turnstile_verify(getattr(data, "turnstile_token", ""), client_ip):
        raise HTTPException(status_code=400, detail="Verificarea anti-bot a eșuat")

    if not await ip_can_register(client_ip):
        raise HTTPException(status_code=429,
                            detail="Prea multe conturi create de pe această rețea. Încearcă mâine.")

    db = get_db()
    existing = await db.users.find_one({"email": data.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail="Email deja înregistrat")
    
    user = {
        "email":       data.email.lower(),
        "password":    hash_password(data.password),
        "name":        data.name or data.email.split("@")[0],
        "plan":        "free",
        "created_at":  datetime.now(timezone.utc),
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
        "usage":       {},  # {"2025-01-01": 5, ...}
    }
    result = await db.users.insert_one(user)
    user_id = str(result.inserted_id)
    token = create_token(user_id)
    await _register_session(user_id, token)
    return {"token": token, "user_id": user_id}

async def login_user(data: UserLogin) -> dict:
    db = get_db()
    user = await db.users.find_one({"email": data.email.lower()})
    if not user or not verify_password(data.password, user["password"]):
        raise HTTPException(status_code=401, detail="Email sau parolă incorectă")
    user_id = str(user["_id"])
    token = create_token(user_id, user.get("token_version", 0))
    await _register_session(user_id, token)
    return {"token": token, "user_id": user_id}


# ── Concurrent-session control (anti account-sharing on paid plans) ──
MAX_CONCURRENT_SESSIONS = int(os.getenv("MAX_CONCURRENT_SESSIONS", "2"))


def _jwt_jti(token: str) -> str:
    try:
        return decode_token(token).get("jti", "")
    except Exception:
        return ""


async def _register_session(user_id: str, token: str):
    """
    Track active sessions per user in Redis (TTL = token lifetime).
    Keeps only the newest MAX_CONCURRENT_SESSIONS valid; older logins are
    evicted immediately -> shared accounts stop working on the 3rd login.
    Legacy tokens without jti bypass enforcement entirely.
    """
    jti = _jwt_jti(token)
    if not jti:
        return
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if not redis:
        return
    try:
        sess_key = f"ts:sess:{user_id}"
        ttl_seconds = TOKEN_EXPIRE * 60
        old = await redis.lrange(sess_key, 0, -1)

        pipe = redis.pipeline()
        pipe.rpush(sess_key, jti)
        pipe.ltrim(sess_key, -MAX_CONCURRENT_SESSIONS, -1)
        pipe.expire(sess_key, ttl_seconds)
        pipe.setex(f"ts:jti:{user_id}:{jti}", ttl_seconds, 1)
        await pipe.execute()

        # Explicitly kill evicted sessions (their jti keys would otherwise
        # survive until TTL and keep account-sharing alive).
        kept = set(await redis.lrange(sess_key, 0, -1))
        for old_jti in old:
            if old_jti not in kept:
                await redis.delete(f"ts:jti:{user_id}:{old_jti}")
                print(f"[SESSION] Evicted old session {old_jti[:8]}… for user {user_id}")
    except Exception as e:
        print(f"[WARN] Session registration skipped: {e}")

async def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Dependency -- returns user dict or None if not authenticated.

    Supports BOTH JWT tokens (from login) and API keys (widgets/extensions):
      - JWT:  Authorization: Bearer <jwt_token>
      - API:  Authorization: Bearer <ts_...>  OR  X-API-Key: <ts_...>
    """
    if not credentials:
        return None
    token = credentials.credentials

    if token.startswith("ts_"):
        try:
            from utils.api_keys import validate_api_key
            return await validate_api_key(token)
        except Exception:
            return None

    try:
        from bson import ObjectId
        payload = decode_token(token)
        user_id = payload.get("sub")
        # ── Session enforcement (kills account sharing on paid plans) ──
        # Only applies to NEW tokens that carry a jti. Legacy tokens
        # (issued before this feature) keep working — no forced logouts.
        jti = payload.get("jti")
        if jti and os.getenv("SESSION_ENFORCE", "on").lower() in ("1", "true", "on"):
            try:
                from utils.redis_client import get_async_redis
                redis = get_async_redis()
                if redis and not await redis.exists(f"ts:jti:{user_id}:{jti}"):
                    return None   # session was evicted by newer logins
            except Exception:
                pass   # Redis down -> fail-open (availability > strictness)
        db = get_db()
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if user:
            # Token-version gate: a logout-everywhere / password change bumps
            # user.token_version, instantly voiding every token minted with the
            # old value. Enforced straight off the doc (no Redis dependency).
            if int(payload.get("tv", 0)) != int(user.get("token_version", 0)):
                return None
            user["id"] = str(user["_id"])
        return user
    except Exception:
        return None


async def logout_user(credentials, all_devices: bool = False) -> dict:
    """Real logout.

    Default: revoke ONLY the current session — delete this token's jti key and
    drop it from the user's Redis session list, so the exact token stops working
    immediately (other devices stay logged in). If Redis is down this is a
    best-effort no-op, which is why…

    all_devices=True: bump user.token_version in Mongo. Every outstanding token
    (this device and all others) carried the old `tv` and is voided at once —
    the DB-backed path that works with or without Redis. Use for
    "log out everywhere" and after a password reset.
    """
    if not credentials:
        return {"status": "ok"}
    token = credentials.credentials
    try:
        payload = decode_token(token)
    except Exception:
        return {"status": "ok"}   # already invalid/expired -> nothing to do
    user_id = payload.get("sub", "")
    jti     = payload.get("jti", "")

    if all_devices and user_id:
        try:
            from bson import ObjectId
            db = get_db()
            await db.users.update_one({"_id": ObjectId(user_id)},
                                      {"$inc": {"token_version": 1}})
        except Exception as e:
            print(f"[LOGOUT] token_version bump failed: {e}")
        # Also clear the whole Redis session list if present.
        try:
            from utils.redis_client import get_async_redis
            redis = get_async_redis()
            if redis:
                sess_key = f"ts:sess:{user_id}"
                for j in await redis.lrange(sess_key, 0, -1):
                    await redis.delete(f"ts:jti:{user_id}:{j}")
                await redis.delete(sess_key)
        except Exception:
            pass
        return {"status": "ok", "scope": "all_devices"}

    # Single-session logout: revoke just this jti.
    if user_id and jti:
        try:
            from utils.redis_client import get_async_redis
            redis = get_async_redis()
            if redis:
                await redis.delete(f"ts:jti:{user_id}:{jti}")
                await redis.lrem(f"ts:sess:{user_id}", 0, jti)
        except Exception as e:
            print(f"[LOGOUT] session revoke skipped: {e}")
    return {"status": "ok", "scope": "session"}


async def require_user(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Dependency -- raises 401 if not authenticated."""
    user = await get_current_user(credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Autentificare necesara")
    return user

async def check_rate_limit(user, claim, client_ip: str = "") -> dict:
    """Check rate limit. Anonymous visitors get ANON_DAILY_CAP checks/day/IP."""
    if user is None:
        # Anonymous: small try-before-signup allowance per IP
        from utils.abuse import anon_ip_check
        _, info = await anon_ip_check(client_ip)
        return info

    plan_name = user.get("plan", "free")
    plan      = PLANS.get(plan_name, PLANS["free"])
    limit     = plan["daily_limit"]
    today     = today_key()
    used      = user.get("usage", {}).get(today, 0)
    db = get_db()

    # Try Redis first (atomic, fast, shared)
    redis_result = await _redis_rate_check(user, used, limit, plan_name)
    if redis_result is not None:
        # Eco-mode flag is computed here (single source) regardless of which
        # backend produced the dict — rate_limiter's Redis path doesn't set it.
        r_used = redis_result.get("used", used)
        redis_result["eco"] = r_used > _ECO_AFTER.get(plan_name, 999999)
        return redis_result

    # Fallback: MongoDB atomic increment.
    # NOTE: `$lt` alone does NOT match documents where usage.<today> is absent
    # (MongoDB range operators skip missing fields), which is the norm for a new
    # user or the first check of a new UTC day — that wrongly blocked the first
    # request. The `$or` with `$exists:false` handles the missing-field case so
    # the very first check of the day is allowed and the field gets created.
    from bson import ObjectId
    from pymongo import ReturnDocument
    result = await db.users.find_one_and_update(
        {"_id": ObjectId(user["id"]),
         "$or": [
             {f"usage.{today}": {"$exists": False}},
             {f"usage.{today}": {"$lt": limit}},
         ]},
        {"$inc": {f"usage.{today}": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return {"allowed": False, "used": limit, "limit": limit,
                "plan": plan_name, "reset_in_hours": _hours_until_midnight_utc(),
                "features": plan.get("features", {})}
    new_used = result.get("usage", {}).get(today, used + 1)
    return {"allowed": True, "used": new_used, "limit": limit,
            "plan": plan_name, "reset_in_hours": _hours_until_midnight_utc(),
            "eco": new_used > _ECO_AFTER.get(plan_name, 999999),
            "features": plan.get("features", {})}


async def _current_daily_used(user) -> int:
    """
    Return the REAL count of checks used today for this user.

    Enforcement runs against the Redis counter (ts:rl:<id>:<date>) whenever
    Redis is up, and only falls back to the Mongo `usage.<date>` field when it
    is down. Reading Mongo alone (as get_user_out used to) reported 0 while the
    true counter lived in Redis — so the dashboard quota never moved. Prefer
    Redis, fall back to Mongo.
    """
    today = today_key()
    user_id = str(user.get("id") or user.get("_id") or "")
    try:
        from utils.redis_client import get_async_redis
        redis = get_async_redis()
        if redis and user_id:
            val = await redis.get(f"ts:rl:{user_id}:{today}")
            if val is not None:
                return int(val)
    except Exception:
        pass
    return user.get("usage", {}).get(today, 0)


async def get_user_out(user):
    plan_name = user.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS["free"])
    return UserOut(
        id=str(user["_id"]),
        email=user["email"],
        name=user.get("name", ""),
        plan=plan_name,
        daily_limit=plan["daily_limit"],
        used_today=await _current_daily_used(user),
        stripe_customer_id=user.get("stripe_customer_id", ""),
    )


async def upgrade_user_plan(user_id: str, plan: str, stripe_customer_id: str = "", subscription_id: str = ""):
    """Called from Stripe webhook to upgrade user plan."""
    db = get_db()
    from bson import ObjectId
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {
            "plan": plan,
            "stripe_customer_id": stripe_customer_id,
            "stripe_subscription_id": subscription_id,
        }}
    )



async def _redis_rate_check(user, used_in_doc, limit, plan_name):
    """
    Try Redis for atomic rate-limiting.
    Adds earned feedback bonuses on top of the plan limit.
    Returns info dict or None (Redis unavailable).
    """
    try:
        from utils.rate_limiter import check_rate_limit as _rl
        from utils.abuse import get_feedback_bonus
        allowed, info = await _rl(user)
        # Gamification: +N bonus checks earned via feedback (free tier hook)
        uid = str(user.get("id") or "")
        bonus = await get_feedback_bonus(uid)
        if bonus > 0:
            info["limit"] = info.get("limit", limit) + bonus
            info["allowed"] = info.get("used", 0) <= info["limit"]
            info["bonus_checks"] = bonus
        return info
    except Exception:
        return None


def _hours_until_midnight_utc():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Next UTC midnight — quotas reset at the start of the *next* day, not today's
    # (which is already in the past and would give a negative number).
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((next_midnight - now).total_seconds() // 3600))

AUTH_AVAILABLE = True


# ── Google OAuth verification + login helper ───────────────────
# Allowed audiences: the web client plus any extra client IDs (extension,
# mobile) listed in GOOGLE_CLIENT_IDS (comma-separated). A Google ID token is
# only accepted if its `aud` is in this set — this is what stops a token minted
# for a different app from logging into a TruthScore account.
_GOOGLE_CLIENT_IDS = {c.strip() for c in (
    os.getenv("GOOGLE_CLIENT_ID", "") + "," + os.getenv("GOOGLE_CLIENT_IDS", "")
).split(",") if c.strip()}


async def _verify_google_id_token(token: str) -> dict:
    """Verify a Google ID token OFFLINE (signature + exp + iss) and enforce aud.

    Returns the validated claims dict, or raises HTTPException. The audience
    check is done here (not inside verify_oauth2_token) so we can accept several
    of our own client IDs (web/extension) without accepting anyone else's.
    """
    if not token:
        raise HTTPException(401, "Missing Google token")
    if not _GOOGLE_CLIENT_IDS:
        raise HTTPException(500, "Google OAuth not configured (set GOOGLE_CLIENT_ID)")
    try:
        from google.oauth2 import id_token as _gid
        from google.auth.transport import requests as _greq
        import asyncio as _aio
        loop = _aio.get_event_loop()
        claims = await loop.run_in_executor(
            None, lambda: _gid.verify_oauth2_token(token, _greq.Request()))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"Invalid Google ID token: {str(e)[:80]}")
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "Untrusted Google token issuer")
    if claims.get("aud") not in _GOOGLE_CLIENT_IDS:
        raise HTTPException(401, "Google token was not issued for this application")
    if not claims.get("email_verified", False):
        raise HTTPException(401, "Google email not verified")
    if not claims.get("email"):
        raise HTTPException(400, "No email in Google token")
    return claims


async def _google_login_or_register(email: str, name: str) -> dict:
    """Find-or-create a Google-authenticated user and mint a TruthScore JWT."""
    if not email:
        raise HTTPException(400, "Email negăsit în contul Google")
    db = get_db()
    user = await db.users.find_one({"email": email})
    if not user:
        from datetime import datetime as _dt, timezone as _tz
        new_user = {
            "email": email, "password": "", "name": name,
            "plan": "free", "created_at": _dt.now(_tz.utc),
            "auth_provider": "google",
            "stripe_customer_id": "", "stripe_subscription_id": "",
            "usage": {},
        }
        result = await db.users.insert_one(new_user)
        user_id = str(result.inserted_id)
        _tv = 0
    else:
        user_id = str(user["_id"])
        _tv = user.get("token_version", 0)
        if name and user.get("name") != name:
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"name": name, "auth_provider": "google"}})
    token = create_token(user_id, _tv)
    await _register_session(user_id, token)
    return {"token": token, "user_id": user_id, "email": email, "name": name}


async def google_auth(req: GoogleAuthRequest):
    """Verify a Google ID token and login/register the user.

    The client sends a Google *ID token* (a signed JWT, e.g. the `credential`
    from Google Identity Services). We verify its signature, issuer and —
    critically — that its audience (`aud`) is one of OUR client IDs. Previously
    this endpoint accepted any bearer token and merely called Google's userinfo,
    so a token minted for an unrelated Google app could be replayed to log into
    the victim's account (token-substitution takeover).
    """
    if not AUTH_AVAILABLE:
        raise HTTPException(503, "Auth not configured")
    claims = await _verify_google_id_token(req.token)
    email = (claims.get("email") or "").lower()
    name  = claims.get("name", "") or claims.get("given_name", "")
    return await _google_login_or_register(email, name)


async def google_callback():
    """Handle Google OAuth callback for web dashboard (authorization code + PKCE flow)."""
    return HTMLResponse(content="""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>TruthScore -- Autentificare</title></head>
<body style="background:#06060e;color:#eeeef8;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh">
<script>
// Authorization code flow: code arrives as query param, code_verifier stored in sessionStorage
const params = new URLSearchParams(window.location.search);
const code = params.get('code');
const error = params.get('error');

if (error) {
  document.getElementById('msg').textContent = 'Eroare Google: ' + error;
} else if (code) {
  const codeVerifier = sessionStorage.getItem('ts_pkce_verifier');
  const redirectUri = window.location.origin + '/auth/google/callback';
  sessionStorage.removeItem('ts_pkce_verifier');
  fetch('/auth/google/exchange', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code, code_verifier: codeVerifier, redirect_uri: redirectUri})
  })
  .then(r => r.json())
  .then(d => {
    if (d.token) {
      localStorage.setItem('ts_token', d.token);
      window.location.href = '/';
    } else {
      document.getElementById('msg').textContent = 'Eroare: ' + (d.detail || 'necunoscută');
    }
  })
  .catch(e => {
    document.getElementById('msg').textContent = 'Eroare conexiune: ' + e.message;
  });
} else {
  document.getElementById('msg').textContent = 'Cod Google negăsit. Încearcă din nou.';
}
</script>
<div id="msg" style="font-size:16px">[loading] Se autentifică cu Google...</div>
</body></html>""")


async def google_exchange(code: str, code_verifier: str, redirect_uri: str):
    """Exchange authorization code + PKCE verifier for a TruthScore JWT.
    Called by the dashboard after Google redirects back to "/" with ?code=."""
    import os as _os
    client_id     = _os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = _os.getenv("GOOGLE_CLIENT_SECRET") or _os.getenv("GOOGLE_WEB_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise HTTPException(500, "Google OAuth not configured (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env)")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Step 1: exchange code for access token
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code":          code,
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "redirect_uri":  redirect_uri,
                    "grant_type":    "authorization_code",
                    "code_verifier": code_verifier or "",
                },
            )
            if token_resp.status_code != 200:
                raise HTTPException(401, f"Google token exchange failed: {token_resp.text[:200]}")
            tok_json = token_resp.json()
            id_tok       = tok_json.get("id_token", "")
            access_token = tok_json.get("access_token", "")

        # Preferred path: the exchange returns a signed ID token. Verify it
        # (signature + iss + aud) — same guarantee as the /auth/google path.
        if id_tok:
            try:
                claims = await _verify_google_id_token(id_tok)
                email = (claims.get("email") or "").lower()
                name  = claims.get("name", "") or claims.get("given_name", "")
                return await _google_login_or_register(email, name)
            except HTTPException:
                # Fall through to userinfo only if verification setup is missing
                # (e.g. GOOGLE_CLIENT_ID unset); real token forgery still can't
                # reach here because the code came from a client_secret exchange.
                if _GOOGLE_CLIENT_IDS:
                    raise

        # Fallback: no id_token returned — use the access token against userinfo.
        # Safe because access_token came from a server-side client_secret exchange.
        if not access_token:
            raise HTTPException(401, "No token in Google response")
        async with httpx.AsyncClient(timeout=15.0) as client:
            user_resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code != 200:
                raise HTTPException(401, "Failed to fetch Google user info")
            info = user_resp.json()

        # Same guarantee as the id-token path (line 640): never create/log in an
        # account off an unverified email. userinfo returns email_verified as a
        # bool OR the string "true" depending on the scope/endpoint — accept both.
        _ev = info.get("email_verified", info.get("verified_email", False))
        if not (_ev is True or str(_ev).lower() == "true"):
            raise HTTPException(401, "Google email not verified")
        email = info.get("email", "").lower()
        if not email:
            raise HTTPException(400, "No email in Google user info")
        name  = info.get("name", "") or info.get("given_name", "")
        return await _google_login_or_register(email, name)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Eroare Google OAuth exchange: {str(e)[:100]}")