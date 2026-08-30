"""
TruthScore Authentication & User Management
MongoDB + JWT + Rate Limiting + Stripe Plans
"""
import os, time, hashlib, secrets, asyncio
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
    "free":            int(os.getenv("PLAN_FREE_DAILY", "3")),
    "pro":             int(os.getenv("PLAN_PRO_DAILY", "200")),
    "annual_pro":      int(os.getenv("PLAN_PRO_DAILY", "200")),
    "monitor":         int(os.getenv("PLAN_MONITOR_DAILY", "500")),
    "annual_monitor":  int(os.getenv("PLAN_MONITOR_DAILY", "500")),
    "business":        int(os.getenv("PLAN_BUSINESS_DAILY", "800")),
    "annual_business": int(os.getenv("PLAN_BUSINESS_DAILY", "800")),
    "enterprise":      int(os.getenv("PLAN_ENTERPRISE_DAILY", "9999")),
}
# New users get this many checks/day during trial (first 7 days)
_TRIAL_DAILY   = int(os.getenv("PLAN_TRIAL_DAILY", "10"))
_TRIAL_DAYS    = int(os.getenv("TRIAL_DAYS", "7"))

# After this many checks in a day, routing switches to eco mode (cheap
# models, no paid search) — protects margin from heavy-day users without
# any hard block. Free never reaches it. Business threshold lower than
# Pro because paragraphs let power users burn hundreds of fresh claims/day;
# at €29.99 the absolute-worst case must still land near break-even.
_ECO_AFTER = {
    "free":            999999,
    "pro":             int(os.getenv("PRO_ECO_AFTER", "100")),
    "annual_pro":      int(os.getenv("PRO_ECO_AFTER", "100")),
    "monitor":         int(os.getenv("MONITOR_ECO_AFTER", "250")),
    "annual_monitor":  int(os.getenv("MONITOR_ECO_AFTER", "250")),
    "business":        int(os.getenv("BUSINESS_ECO_AFTER", "350")),
    "annual_business": int(os.getenv("BUSINESS_ECO_AFTER", "350")),
    "enterprise":      999999,
}

_PLAN_PRICES = {
    "free":            0,
    "pro":             9.99,
    "annual_pro":      79.99,
    "monitor":         99.0,
    "annual_monitor":  790.0,
    "business":        29.99,
    "annual_business": 239.88,
    "enterprise":      199,
}

_PLAN_FEATURES = {
    "free":            {"batch": False, "pdf": False, "widget": False, "seats": 1,
                        "api_quota": 0,    "api_keys": 1,   "models": ["eco"],     "monitors": 0},
    "pro":             {"batch": True,  "pdf": True,  "widget": True,  "seats": 1,
                        "api_quota": 0,    "api_keys": 5,   "models": ["gemini", "groq"], "monitors": 0},
    "annual_pro":      {"batch": True,  "pdf": True,  "widget": True,  "seats": 1,
                        "api_quota": 0,    "api_keys": 5,   "models": ["gemini", "groq"], "monitors": 0},
    "monitor":         {"batch": True,  "pdf": True,  "widget": True,  "seats": 2,
                        "api_quota": 1000, "api_keys": 10,  "models": ["gemini", "groq"], "monitors": 5},
    "annual_monitor":  {"batch": True,  "pdf": True,  "widget": True,  "seats": 2,
                        "api_quota": 1000, "api_keys": 10,  "models": ["gemini", "groq"], "monitors": 5},
    "business":        {"batch": True,  "pdf": True,  "widget": True,  "seats": 3,
                        "api_quota": 5000, "api_keys": 20,  "models": ["gemini", "groq", "gpt4o-mini"], "monitors": 20},
    "annual_business": {"batch": True,  "pdf": True,  "widget": True,  "seats": 3,
                        "api_quota": 5000, "api_keys": 20,  "models": ["gemini", "groq", "gpt4o-mini"], "monitors": 20},
    "enterprise":      {"batch": True,  "pdf": True,  "widget": True,  "seats": 0,
                        "api_quota": -1,   "api_keys": 100, "models": ["all"],               "monitors": -1},
}


def _get_plans():
    return {
        "free": {
            "name": "Free", "price": _PLAN_PRICES["free"],
            "daily_limit": _PLAN_DAILY["free"],
            "batch_limit": 0, "pdf": False, "widget": False,
            "price_id": None,
            "features": _PLAN_FEATURES["free"],
            "ads": True,
            "bonus_via_feedback": True,
            "trial_daily": _TRIAL_DAILY,
            "trial_days": _TRIAL_DAYS,
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
        "annual_pro": {
            "name": "Pro Annual", "price": _PLAN_PRICES["annual_pro"],
            "daily_limit": _PLAN_DAILY["annual_pro"],
            "batch_limit": 50, "pdf": True, "widget": True,
            "price_id": os.getenv("STRIPE_PRO_ANNUAL_PRICE_ID", ""),
            "features": _PLAN_FEATURES["annual_pro"],
            "ads": False,
            "eco_after_daily": _ECO_AFTER["annual_pro"],
            "billing": "annual",
            "saves_percent": 33,
        },
        "monitor": {
            "name": "Monitor", "price": _PLAN_PRICES["monitor"],
            "daily_limit": _PLAN_DAILY["monitor"],
            "batch_limit": 200, "pdf": True, "widget": True,
            "price_id": os.getenv("STRIPE_MONITOR_PRICE_ID", ""),
            "features": _PLAN_FEATURES["monitor"],
            "ads": False,
            "eco_after_daily": _ECO_AFTER["monitor"],
            "highlight": "Pentru jurnaliști și companii",
        },
        "annual_monitor": {
            "name": "Monitor Annual", "price": _PLAN_PRICES["annual_monitor"],
            "daily_limit": _PLAN_DAILY["annual_monitor"],
            "batch_limit": 200, "pdf": True, "widget": True,
            "price_id": os.getenv("STRIPE_MONITOR_ANNUAL_PRICE_ID", ""),
            "features": _PLAN_FEATURES["annual_monitor"],
            "ads": False,
            "eco_after_daily": _ECO_AFTER["annual_monitor"],
            "billing": "annual",
            "saves_percent": 33,
            "highlight": "Pentru jurnaliști și companii",
        },
        "business": {
            "name": "Business", "price": _PLAN_PRICES["business"],
            "daily_limit": _PLAN_DAILY["business"],
            "batch_limit": 500, "pdf": True, "widget": True,
            "price_id": os.getenv("STRIPE_BUSINESS_PRICE_ID", ""),
            "features": _PLAN_FEATURES["business"],
            "ads": False,
            "eco_after_daily": _ECO_AFTER["business"],
        },
        "annual_business": {
            "name": "Business Annual", "price": _PLAN_PRICES["annual_business"],
            "daily_limit": _PLAN_DAILY["annual_business"],
            "batch_limit": 500, "pdf": True, "widget": True,
            "price_id": os.getenv("STRIPE_BUSINESS_ANNUAL_PRICE_ID", ""),
            "features": _PLAN_FEATURES["annual_business"],
            "ads": False,
            "eco_after_daily": _ECO_AFTER["annual_business"],
            "billing": "annual",
            "saves_percent": 33,
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

# Connection-pool settings — 25 per worker × 4 workers = 100 total (fits M2's 200-connection limit)
_MONGO_POOL_SIZE = int(os.getenv("MONGO_POOL_SIZE", "25"))


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
    bonus_today: int = 0
    ref_code: str = ""
    trial_active: bool = False
    streak: int = 0
    streak_best: int = 0

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


# ── Effective daily limit (SINGLE SOURCE OF TRUTH) ─────────────
# Every path that needs a user's per-day check allowance — the Mongo fallback
# here, the Redis counting path in utils.rate_limiter, and get_user_out — MUST
# call this ONE function so the base-plan limit + free-trial bump + permanent
# referral (bonus_checks) can never drift between code paths. (The daily,
# ephemeral *feedback* bonus is added dynamically on top by the callers that
# have Redis, since it isn't stored on the user doc.)
def _is_trial_active(user) -> bool:
    """True if this free user is still inside their signup trial window."""
    if (user or {}).get("plan", "free") != "free":
        return False
    trial_until = user.get("trial_until")
    if not trial_until:
        return False
    if isinstance(trial_until, str):
        try:
            trial_until = datetime.fromisoformat(trial_until.replace("Z", "+00:00"))
        except Exception:
            return False
    return bool(trial_until and datetime.now(timezone.utc) < trial_until)


def get_effective_daily_limit(user) -> int:
    """The REAL per-day check limit for a user = base plan limit
    + trial bump (free tier, first N days) + permanent referral bonus_checks.

    This is the single source of truth used by BOTH the auth Mongo-fallback path
    and utils.rate_limiter's Redis path, so trial/referral bonuses are enforced
    identically everywhere. Does NOT include the daily feedback bonus (added
    separately by callers that can read it from Redis)."""
    plan_name = (user or {}).get("plan", "free")
    plan = PLANS.get(plan_name, PLANS["free"])
    limit = plan["daily_limit"]
    if _is_trial_active(user):
        limit = max(limit, _TRIAL_DAILY)
    limit += int(user.get("bonus_checks", 0))
    return limit


# ── Password strength (registration + reset) ───────────────────
# Tiny embedded set of the most-abused passwords; the goal is to reject the
# obvious garbage, not to be draconian. Extend via env if ever needed.
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwerty123", "111111111", "iloveyou", "letmein1",
    "admin123", "welcome1", "changeme", "qwertyuiop", "1q2w3e4r",
}


def validate_password_strength(pw: str) -> None:
    """Raise HTTPException(400) when a password is too weak. Rules kept
    reasonable: >= 8 chars, not all-numeric, not a well-known common password."""
    if not pw or len(pw) < 8:
        raise HTTPException(status_code=400,
                            detail="Parola trebuie să aibă cel puțin 8 caractere")
    if pw.isdigit():
        raise HTTPException(status_code=400,
                            detail="Parola nu poate fi formată doar din cifre")
    if pw.strip().lower() in _COMMON_PASSWORDS:
        raise HTTPException(status_code=400,
                            detail="Parola este prea comună — alege una mai puternică")


# ── Auth endpoints helpers ─────────────────────────────────────
async def register_user(data: UserRegister, client_ip: str = "") -> dict:
    """Register a new account, with abuse checks (disposable email,
    Turnstile CAPTCHA, per-IP velocity)."""
    from utils.abuse import is_disposable_email, turnstile_verify, ip_can_register

    validate_password_strength(data.password)

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
    
    _email_token = secrets.token_urlsafe(32)
    _ref_code = secrets.token_urlsafe(6)[:8].upper()
    _trial_until = datetime.now(timezone.utc) + timedelta(days=_TRIAL_DAYS)
    user = {
        "email":       data.email.lower(),
        "password":    hash_password(data.password),
        "name":        data.name or data.email.split("@")[0],
        "plan":        "free",
        "created_at":  datetime.now(timezone.utc),
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
        "usage":       {},
        "email_verified": False,
        "email_token": _email_token,
        "ref_code":    _ref_code,
        "trial_until": _trial_until,
        "bonus_checks": 0,
        "streak": 0,
        "streak_best": 0,
        "last_active_date": "",
    }
    result = await db.users.insert_one(user)
    user_id = str(result.inserted_id)
    token = create_token(user_id)
    await _register_session(user_id, token)
    try:
        from config import get_public_base_url
        _base = get_public_base_url()
        _link = f"{_base}/auth/verify-email?token={_email_token}"
        _html = (
            f"<p>Bun venit la TruthScore!</p>"
            f"<p>Verifică adresa de email apăsând linkul de mai jos:</p>"
            f'<p><a href="{_link}">{_link}</a></p>'
        )
        from utils.mailer import send_email as _send_email
        asyncio.create_task(_send_email(data.email.lower(), "Verifică adresa de email — TruthScore", _html))
    except Exception as _e:
        print(f"[AUTH] Verification email skipped (non-fatal): {_e}")
    return {"token": token, "user_id": user_id}


async def apply_referral(ref_code: str, new_user_id: str) -> bool:
    """Credit 5 bonus checks to the referrer and 5 to the new user. Idempotent."""
    if not ref_code or not new_user_id:
        return False
    try:
        from bson import ObjectId
        db = get_db()
        referrer = await db.users.find_one({"ref_code": ref_code.upper()})
        if not referrer:
            return False
        referrer_id = referrer["_id"]
        # Don't allow self-referral
        if str(referrer_id) == new_user_id:
            return False
        # Check not already applied
        already = await db.referrals.find_one({"referrer": str(referrer_id), "referred": new_user_id})
        if already:
            return False
        await db.referrals.insert_one({
            "referrer": str(referrer_id), "referred": new_user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        # +5 checks to referrer
        await db.users.update_one({"_id": referrer_id}, {"$inc": {"bonus_checks": 5}})
        # +5 checks to new user
        await db.users.update_one({"_id": ObjectId(new_user_id)}, {"$inc": {"bonus_checks": 5}})
        return True
    except Exception as e:
        print(f"[AUTH] apply_referral error: {e}")
        return False


async def login_user(data: UserLogin, client_ip: str = "") -> dict:
    """Authenticate a user, with brute-force throttling (per-IP + per-account).

    `client_ip` is optional for backward compatibility; pass it from the endpoint
    (main.py: `login_user(data, client_ip=_client_ip(request))`) to also throttle
    by IP. Per-account throttling works even without it."""
    from utils.abuse import (check_login_throttle, record_login_failure,
                             clear_login_attempts)
    email = data.email.lower()
    await check_login_throttle(client_ip, email)   # raises 429 if locked out
    db = get_db()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(data.password, user["password"]):
        await record_login_failure(client_ip, email)
        raise HTTPException(status_code=401, detail="Email sau parolă incorectă")
    await clear_login_attempts(client_ip, email)
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

async def check_rate_limit(user, claim, client_ip: str = "", fp: str = "") -> dict:
    """Check rate limit. Anonymous visitors get ANON_DAILY_CAP checks/day/IP+fingerprint."""
    if user is None:
        from utils.abuse import anon_ip_check
        _, info = await anon_ip_check(client_ip, fp)
        return info

    plan_name = user.get("plan", "free")
    plan      = PLANS.get(plan_name, PLANS["free"])
    # Effective limit = base plan + trial bump + referral bonus (single source).
    limit     = get_effective_daily_limit(user)

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
        {"_id": ObjectId(str(user.get("id") or user.get("_id", ""))),
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
    try:
        from utils.abuse import get_feedback_bonus
        bonus = await get_feedback_bonus(str(user["_id"]))
    except Exception:
        bonus = 0
    # Referral bonus (permanent daily limit increase)
    referral_bonus = int(user.get("bonus_checks", 0))
    # Trial status + effective limit come from the single-source helpers so the
    # dashboard shows exactly what the rate limiter enforces.
    trial_active = _is_trial_active(user)
    effective_limit = get_effective_daily_limit(user)
    return UserOut(
        id=str(user["_id"]),
        email=user["email"],
        name=user.get("name", ""),
        plan=plan_name,
        daily_limit=effective_limit,
        used_today=await _current_daily_used(user),
        stripe_customer_id=user.get("stripe_customer_id", ""),
        bonus_today=bonus + referral_bonus,
        ref_code=user.get("ref_code", ""),
        trial_active=trial_active,
        streak=int(user.get("streak", 0)),
        streak_best=int(user.get("streak_best", 0)),
    )


async def touch_streak(user_id: str) -> dict:
    """Record daily activity and update the user's consecutive-day streak.

    Called on any meaningful action (a fact-check, a challenge answer). Idempotent
    within a single UTC day. Returns {streak, streak_best, advanced} where
    `advanced` is True only on the first activity of a new day.
    """
    from bson import ObjectId
    try:
        db = get_db()
        try:
            oid = ObjectId(str(user_id))
        except Exception:
            return {"streak": 0, "streak_best": 0, "advanced": False}
        user = await db.users.find_one({"_id": oid})
        if not user:
            return {"streak": 0, "streak_best": 0, "advanced": False}

        today = datetime.now(timezone.utc).date()
        last = user.get("last_active_date") or ""
        streak = int(user.get("streak", 0))
        best = int(user.get("streak_best", 0))

        if last == today.isoformat():
            return {"streak": streak, "streak_best": best, "advanced": False}

        last_date = None
        if last:
            try:
                last_date = datetime.fromisoformat(last).date()
            except Exception:
                last_date = None

        if last_date is not None and (today - last_date).days == 1:
            streak += 1
        else:
            streak = 1  # first ever, or a gap broke the chain

        best = max(best, streak)
        await db.users.update_one(
            {"_id": oid},
            {"$set": {"streak": streak, "streak_best": best,
                      "last_active_date": today.isoformat()}},
        )
        return {"streak": streak, "streak_best": best, "advanced": True}
    except Exception as e:
        print(f"[AUTH] touch_streak error (non-fatal): {e}")
        return {"streak": 0, "streak_best": 0, "advanced": False}


async def upgrade_user_plan(user_id: str, plan: str, stripe_customer_id: str = "", subscription_id: str = ""):
    """Called from Stripe webhook to upgrade user plan.

    Only writes stripe_customer_id / stripe_subscription_id when a non-empty value
    is supplied. An event that omits them (or a redelivery with blanks) must NOT
    blank out ids we already stored — that would orphan the user from their Stripe
    Customer and break every customer-id-keyed webhook that follows.
    """
    db = get_db()
    from bson import ObjectId
    updates = {"plan": plan}
    if stripe_customer_id:
        updates["stripe_customer_id"] = stripe_customer_id
    if subscription_id:
        updates["stripe_subscription_id"] = subscription_id
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": updates},
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
        print(f"[GOOGLE-OAUTH] ID-token verify error: {type(e).__name__}: {str(e)[:300]}")
        raise HTTPException(401, "Invalid Google token")
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
        # ── Account pre-hijack defense ──────────────────────────────
        # Google has just PROVEN this person controls `email`. If the existing
        # account still carries a local password (someone registered this email
        # with a password but never proved they own it), that password could be
        # an attacker who pre-registered the victim's address to lie in wait.
        # Since Google verified ownership, the Google user is the legitimate
        # owner: wipe the password so the pre-set credential can't log in, and
        # bump token_version to instantly void any session the pre-registrant
        # opened. A genuine owner simply uses "reset password" or Google going
        # forward — no legitimate access is lost.
        updates = {"auth_provider": "google"}
        if user.get("password"):
            updates["password"] = ""
            _tv = int(_tv) + 1
            updates["token_version"] = _tv
            print(f"[SECURITY] Google merge cleared pre-existing password for "
                  f"user {user_id} (pre-hijack defense); sessions voided.")
        if name and user.get("name") != name:
            updates["name"] = name
        await db.users.update_one({"_id": user["_id"]}, {"$set": updates})
    token = create_token(user_id, _tv)
    await _register_session(user_id, token)
    return {"token": token, "user_id": user_id, "email": email, "name": name}


async def google_auth(req: GoogleAuthRequest):
    """Verify a Google credential and login/register the user.

    Accepts two token shapes:
    1. ID token (JWT — 3 dot-separated parts): verified offline via Google's
       public keys. Audience check prevents token-substitution takeover.
    2. Access token (opaque string): comes from chrome.identity.getAuthToken()
       in the Chrome extension. Verified by calling Google's tokeninfo endpoint,
       which returns the token's audience — we enforce aud/azp ∈ our client IDs
       (token-substitution defense) plus email_verified, then read identity.
    """
    if not AUTH_AVAILABLE:
        raise HTTPException(503, "Auth not configured")

    # Detect JWT ID token (3 base64url segments) vs opaque access token
    if req.token.count('.') == 2:
        claims = await _verify_google_id_token(req.token)
        email = (claims.get("email") or "").lower()
        name  = claims.get("name", "") or claims.get("given_name", "")
    else:
        # Access token path (Chrome extension getAuthToken flow).
        # An opaque access token carries no audience of its own, so before we
        # trust it we ask Google's tokeninfo endpoint who it was minted FOR. If
        # its aud/azp isn't one of OUR client IDs, it's a token issued for some
        # OTHER app that the user happened to authorize — accepting it would let
        # that app's operator log into the victim's TruthScore account
        # (token-substitution / confused-deputy). We reject it. tokeninfo also
        # returns email + email_verified, so it doubles as the verified identity.
        async with httpx.AsyncClient(timeout=10.0) as client:
            ti = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": req.token},
            )
        if ti.status_code != 200:
            raise HTTPException(401, "Invalid Google access token")
        ti_json = ti.json()
        # Audience gate — only accept tokens minted for one of our client IDs.
        aud = ti_json.get("aud", "")
        azp = ti_json.get("azp", "")
        if _GOOGLE_CLIENT_IDS and not (aud in _GOOGLE_CLIENT_IDS or azp in _GOOGLE_CLIENT_IDS):
            raise HTTPException(401, "Google token was not issued for this application")
        _ev = ti_json.get("email_verified", False)
        if not (_ev is True or str(_ev).lower() == "true"):
            raise HTTPException(401, "Google email not verified")
        email = (ti_json.get("email") or "").lower()
        if not email:
            raise HTTPException(400, "No email in Google token info")
        # tokeninfo omits display name; fetch it from userinfo (best-effort).
        name = ""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {req.token}"},
                )
                if resp.status_code == 200:
                    info = resp.json()
                    name = info.get("name", "") or info.get("given_name", "")
        except Exception:
            pass

    return await _google_login_or_register(email, name)


async def google_callback():
    """Handle Google OAuth callback for web dashboard (authorization code + PKCE flow)."""
    return HTMLResponse(content="""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>TruthScore</title>
<style>
  body{background:#06060e;color:#eeeef8;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner{width:36px;height:36px;border:3px solid rgba(91,78,255,.2);border-top-color:#5b4eff;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 20px}
  #msg{font-size:15px;color:#b8b8d4;line-height:1.6;text-align:center;max-width:320px}
  .err{color:#fca5a5}
</style>
</head>
<body>
<script>
const params = new URLSearchParams(window.location.search);
const code = params.get('code');
const error = params.get('error');
function setMsg(txt,isErr){
  document.getElementById('msg').innerHTML=txt;
  if(isErr){document.getElementById('spinner').style.display='none';document.getElementById('msg').className='err';}
}
if (error) {
  setMsg('Google sign-in error: ' + error, true);
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
      setMsg('Sign-in failed: ' + (d.detail || 'unknown error'), true);
    }
  })
  .catch(e => {
    setMsg('Connection error: ' + e.message, true);
  });
} else {
  setMsg('No authorization code received. <a href="/" style="color:#5b4eff">Try again</a>', true);
}
</script>
<div style="text-align:center">
  <div class="spinner" id="spinner"></div>
  <div id="msg">Signing in with Google…</div>
</div>
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
                print(f"[GOOGLE-OAUTH] token exchange failed "
                      f"({token_resp.status_code}): {token_resp.text[:300]}")
                raise HTTPException(401, "Google sign-in failed. Please try again.")
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
        print(f"[GOOGLE-OAUTH] exchange error: {type(e).__name__}: {str(e)[:300]}")
        raise HTTPException(500, "Google sign-in failed. Please try again.")


# ── Email verification & password reset ───────────────────────────

async def verify_email_token(token: str) -> bool:
    """Mark the account email_verified=True if the token matches. Returns True on success."""
    db = get_db()
    user = await db.users.find_one({"email_token": token})
    if not user:
        return False
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"email_verified": True}, "$unset": {"email_token": ""}},
    )
    return True


async def forgot_password(email: str, client_ip: str = "") -> bool:
    """Generate a password-reset token and email the link. Always returns True (no email leak).

    Throttled per-IP + per-account so the endpoint can't be used to spam reset
    emails or probe which addresses exist. Pass `client_ip` from the endpoint."""
    from utils.abuse import check_reset_throttle, record_reset_attempt
    await check_reset_throttle(client_ip, email.lower())   # raises 429 if abused
    await record_reset_attempt(client_ip, email.lower())
    db = get_db()
    user = await db.users.find_one({"email": email.lower()})
    if user:
        reset_token = secrets.token_urlsafe(32)
        reset_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"reset_token": reset_token, "reset_token_expires": reset_expires}},
        )
        try:
            from config import get_public_base_url
            _base = get_public_base_url()
            _link = f"{_base}/auth/reset-password?token={reset_token}"
            _html = (
                f"<p>Ai solicitat resetarea parolei TruthScore.</p>"
                f"<p>Apasă linkul de mai jos (valabil 1 oră):</p>"
                f'<p><a href="{_link}">{_link}</a></p>'
                f"<p>Dacă nu ai solicitat acest lucru, ignoră acest email.</p>"
            )
            from utils.mailer import send_email as _send_email
            asyncio.create_task(_send_email(email.lower(), "Resetare parolă — TruthScore", _html))
        except Exception as _e:
            print(f"[AUTH] Password reset email skipped (non-fatal): {_e}")
    return True


async def reset_password(token: str, new_password: str, client_ip: str = "") -> bool:
    """Apply a password reset if the token is valid and not expired.

    Throttled per-IP + per-token so a stolen/guessed reset link can't be
    brute-forced. Also enforces password strength on the new password."""
    from utils.abuse import check_reset_throttle, record_reset_attempt
    await check_reset_throttle(client_ip, token[:24])   # raises 429 if abused
    await record_reset_attempt(client_ip, token[:24])
    validate_password_strength(new_password)
    db = get_db()
    now = datetime.now(timezone.utc)
    user = await db.users.find_one({
        "reset_token": token,
        "reset_token_expires": {"$gt": now},
    })
    if not user:
        raise HTTPException(400, "Token invalid sau expirat")
    new_hash = hash_password(new_password)
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"password": new_hash},
            "$unset": {"reset_token": "", "reset_token_expires": ""},
            "$inc": {"token_version": 1},
        },
    )
    return True