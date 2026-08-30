"""
TruthScore -- Abuse prevention utilities.
Anti-fraud layer for the free tier and paid plans:
  1. Disposable email blocking (embedded list, zero dependencies)
  2. Cloudflare Turnstile verification (free CAPTCHA alternative)
  3. Per-IP registration velocity limits
  4. Feedback-bonus tracking with daily caps + unique-claim enforcement
All Redis-backed where possible, graceful no-op in local dev.
"""
import os, logging

logger = logging.getLogger("truthscore.abuse")

# Environment detection (single source: config._IS_PROD). Used to decide whether
# security layers may fail OPEN (dev convenience) or must fail CLOSED (prod).
# Imported lazily-safe: config never imports this module during its own import,
# so there is no cycle. Falls back to an env sniff if config can't be imported.
try:
    from config import _IS_PROD
except Exception:
    _IS_PROD = os.getenv("ENV", os.getenv("ENVIRONMENT", "dev")).strip().lower() \
        not in ("dev", "development", "local", "localhost", "test", "")

# ── 1. Disposable email domains (top offenders; extend via env) ──
_DISPOSABLE = {
    "mailinator.com","10minutemail.com","guerrillamail.com","guerrillamail.net",
    "yopmail.com","yopmail.net","throwawaymail.com","sharklasers.com","getnada.com",
    "dispostable.com","trashmail.com","fakeinbox.com","maildrop.cc","tempinbox.com",
    "temp-mail.org","tempmail.net","tempmailo.com","mohmal.com","emailondeck.com",
    "spam4.me","grr.la","mytemp.email","binkmail.com","devnullmail.com",
    "mailin8r.com","mailnesia.com","mailsac.com","sogetthis.com",
    "suremail.info","tradermail.info","zippymail.info","1secmail.com",
    "1secmail.net","1secmail.org","esiix.com","wwjmp.com","xojxe.com",
    "yoggm.com","vjuum.com","laafd.com","txcct.com","kzccv.com","qiott.com",
    "ezztt.com","lwsk5.com","fexpost.com","onedmail.com","33mail.com",
    "anonaddy.com","anonaddy.me","simplelogin.io","spamgourmet.com",
    "jetable.org","regbypass.com","mt2015.com","tmpmail.org","tmpmail.net",
    "tempr.email","discard.email","discardmail.com","byom.de","muellmail.com",
    "spambog.com","clrmail.com","gustr.com","zetmail.com","inboxbear.com",
}
_extra = os.getenv("BLOCKED_EMAIL_DOMAINS", "")
if _extra:
    _DISPOSABLE |= {d.strip().lower() for d in _extra.split(",") if d.strip()}


def is_disposable_email(email: str) -> bool:
    """True if the email domain is a known disposable/burner provider."""
    try:
        domain = email.rsplit("@", 1)[1].strip().lower()
        return domain in _DISPOSABLE
    except Exception:
        return False


# ── 2. Cloudflare Turnstile (free, privacy-friendly CAPTCHA) ─────
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET", "")

# In production a missing secret must NOT silently disable the CAPTCHA layer.
# We fail CLOSED there (turnstile_verify returns False → registration is blocked)
# and scream at boot so the misconfiguration is impossible to miss. In dev we
# fail OPEN (allow) for convenience. `_DEV_OPT_IN` is still used by the
# anonymous-quota fallback below.
_DEV_OPT_IN = (os.getenv("DEV_INSECURE", "").lower() in ("1", "true", "yes")
               or os.getenv("ENV", "").lower() in ("development", "dev", "local"))
if not TURNSTILE_SECRET:
    if _IS_PROD:
        logger.error("[SECURITY] TURNSTILE_SECRET unset in PRODUCTION — CAPTCHA "
                     "verification FAILS CLOSED, so registration is BLOCKED until "
                     "you set TURNSTILE_SECRET. Configure it now.")
    else:
        logger.warning("[SECURITY] TURNSTILE_SECRET unset — CAPTCHA bot-protection "
                       "is OFF in dev (fails open). Prod would fail closed.")


async def turnstile_verify(token: str, remoteip: str = "") -> bool:
    """
    Server-side verification of a Turnstile widget token.
    Returns True when the token verifies against a configured secret.

    Fail-open ONLY in dev with no secret configured. In production, a missing
    secret fails CLOSED (returns False) so the CAPTCHA layer can't be silently
    bypassed by forgetting to configure it. When a secret IS set, a missing or
    invalid token always fails.
    """
    if not TURNSTILE_SECRET:
        # No secret: allow in dev (convenience), block in prod (fail closed).
        return not _IS_PROD
    if not token:
        return False
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": TURNSTILE_SECRET,
                      "response": token,
                      **({"remoteip": remoteip} if remoteip else {})},
            )
            return r.status_code == 200 and bool(r.json().get("success"))
    except Exception as e:
        logger.warning("Turnstile verify error (fail-closed): %s", e)
        return False


# ── 3. Per-IP velocity limits ────────────────────────────────────
MAX_REGISTRATIONS_PER_IP_PER_DAY = int(os.getenv("MAX_REGISTRATIONS_PER_IP_PER_DAY", "3"))


def _utc_day() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def ip_can_register(ip: str) -> bool:
    """Max N account creations per IP per day (default 3)."""
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if not redis or not ip:
        return True
    try:
        key = f"ts:ipreg:{ip}:{_utc_day()}"
        used = await redis.incr(key)
        await redis.expire(key, 172800)
        return used <= MAX_REGISTRATIONS_PER_IP_PER_DAY
    except Exception:
        return True


# Cap feedback submissions per IP per day. Feedback feeds the calibration/ECE
# loop, so an unbounded endpoint lets one actor skew the model's confidence
# curve (and hammer the durable store). Generous default — honest users click a
# handful of thumbs per session; only floods hit this.
MAX_FEEDBACK_PER_IP_PER_DAY = int(os.getenv("MAX_FEEDBACK_PER_IP_PER_DAY", "100"))


async def feedback_can_submit(ip: str) -> bool:
    """True if this IP is under the daily feedback cap. Fails OPEN when Redis is
    down: blocking honest thumbs-up/down is worse than a rare unthrottled window,
    since the bonus-check grant is separately capped and unique-claim guarded."""
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if not redis or not ip:
        return True
    try:
        key = f"ts:fbrate:{ip}:{_utc_day()}"
        used = await redis.incr(key)
        await redis.expire(key, 172800)
        return used <= MAX_FEEDBACK_PER_IP_PER_DAY
    except Exception:
        return True


# ── Anonymous try-before-signup quota (per IP) ──────────────────
# Curious visitors get a few free verifications WITHOUT an account —
# the taste that drives signups. Requires Redis; without it (local dev)
# anonymous checks are simply allowed.
# Single source of truth for the anonymous per-IP daily cap. Default 5 (the
# more generous of the two historical values 3/5). `used` here is POST-increment
# (redis.incr returns the new count), so `used <= ANON_DAILY_CAP` allows exactly
# ANON_DAILY_CAP requests and blocks the (ANON_DAILY_CAP+1)-th. All anon sites
# use this constant and this `<=` semantics.
ANON_DAILY_CAP = int(os.getenv("ANON_DAILY_CAP", "5"))


async def anon_ip_check(ip: str, fp: str = ""):
    """Returns (allowed, info_dict) for an anonymous visitor.
    When a browser fingerprint is provided, quota is tracked by IP+fingerprint
    so the same device shares one quota across multiple incognito sessions.
    """
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    base = {"plan": "anonymous", "used": 0, "limit": ANON_DAILY_CAP}
    if not redis or not ip:
        # Without Redis we cannot count anonymous usage per IP. Failing OPEN here
        # gives every anonymous visitor UNLIMITED free verifications the moment
        # Redis is down — a cost/abuse hole in production. So we fail OPEN only in
        # an explicit dev opt-in, and fail CLOSED in prod (anon must sign in; the
        # Mongo-backed quota still serves registered users). Priority: protect the
        # paid LLM spend over anonymous convenience when the counter is missing.
        if _DEV_OPT_IN:
            info = dict(base); info["allowed"] = True
            info["note"] = "no-redis dev mode — anonymous not counted"
            return True, info
        info = dict(base); info["allowed"] = False
        info["note"] = ("anonymous quota unavailable (no Redis) — "
                        "creează un cont gratuit pentru a continua")
        return False, info
    day = _utc_day()
    # Anchor the quota on the SERVER-OBSERVED IP, which the client can't forge
    # (unlike the X-Browser-Fp header). The fingerprint may only NARROW the
    # allowance, never widen it: we count against both the IP key and, when a
    # fingerprint is present, an IP-independent fingerprint key, then enforce the
    # cap on the MAX of the two. This closes two evasion routes at once:
    #   • rotating the fingerprint header  -> the IP counter still climbs
    #   • rotating the IP (VPN/mobile)      -> the fingerprint counter still climbs
    # An attacker must rotate BOTH to gain a single extra check.
    ip_key = f"ts:anon:{ip}:{day}"
    try:
        ip_used = await redis.incr(ip_key)
        await redis.expire(ip_key, 172800)
        used = ip_used
        if fp:
            fp_key = f"ts:anonfp:{fp[:32]}:{day}"
            fp_used = await redis.incr(fp_key)
            await redis.expire(fp_key, 172800)
            used = max(ip_used, fp_used)
        allowed = used <= ANON_DAILY_CAP
        info = dict(base); info.update({"used": used, "allowed": allowed})
        return allowed, info
    except Exception:
        # Redis was reachable at startup but the call failed mid-request. Same
        # reasoning as above: fail CLOSED in prod, open only under dev opt-in.
        if _DEV_OPT_IN:
            info = dict(base); info["allowed"] = True
            return True, info
        info = dict(base); info["allowed"] = False
        info["note"] = "anonymous quota check failed — creează un cont gratuit"
        return False, info


# ── 4. Feedback-bonus checks (gamified calibration data collection) ──
FEEDBACK_BONUS_DAILY_CAP = int(os.getenv("FEEDBACK_BONUS_DAILY_CAP", "10"))


async def grant_feedback_bonus(user_id: str, claim: str) -> dict:
    """
    Award +1 bonus check for unique feedback. Rules:
      - max FEEDBACK_BONUS_DAILY_CAP per day (default 10)
      - each claim rewarded only ONCE (hash set, 7-day window)
    Returns {"granted": bool, "bonus_today": int, ...}
    """
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if not redis or not user_id or user_id.startswith("ts_"):
        return {"granted": False, "bonus_today": 0, "cap": FEEDBACK_BONUS_DAILY_CAP}

    import hashlib
    day = _utc_day()
    claim_hash = hashlib.sha256(claim.strip().lower().encode()).hexdigest()[:24]

    # Unique-claim guard (SADD returns 0 if already rewarded for this claim)
    seen_key = f"ts:fbbseen:{user_id}"
    fresh = await redis.sadd(seen_key, claim_hash)
    await redis.expire(seen_key, 7 * 86400)
    if not fresh:
        return {"granted": False, "bonus_today": -1, "reason": "already_rewarded"}

    count_key = f"ts:fbb:{user_id}:{day}"
    used = await redis.incr(count_key)
    await redis.expire(count_key, 172800)

    if used > FEEDBACK_BONUS_DAILY_CAP:
        await redis.decr(count_key)   # keep counter truthful at the cap
        return {"granted": False, "bonus_today": FEEDBACK_BONUS_DAILY_CAP,
                "cap": FEEDBACK_BONUS_DAILY_CAP, "reason": "daily_cap"}

    return {"granted": True, "bonus_today": used, "cap": FEEDBACK_BONUS_DAILY_CAP}


async def get_feedback_bonus(user_id: str) -> int:
    """How many bonus checks the user earned today (added on top of plan limit)."""
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if not redis or not user_id or user_id.startswith("ts_"):
        return 0
    try:
        raw = await redis.get(f"ts:fbb:{user_id}:{_utc_day()}")
        return min(int(raw or 0), FEEDBACK_BONUS_DAILY_CAP)
    except Exception:
        return 0


# ── 5. Login / password-reset brute-force throttling ────────────
# Fixed-window failure counters keyed on BOTH the client IP and the account
# identifier (email / reset-token). Either key hitting the cap inside the window
# blocks further attempts, so brute force is stopped whether the attacker
# rotates IPs (account key climbs) or sprays accounts from one IP (IP key
# climbs). Redis-backed when available; falls back to a per-process in-memory
# window otherwise (best-effort — good enough on a single instance / dev).
import time as _time

LOGIN_MAX_ATTEMPTS   = int(os.getenv("LOGIN_MAX_ATTEMPTS", "8"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", str(15 * 60)))   # 15 min
RESET_MAX_ATTEMPTS   = int(os.getenv("RESET_MAX_ATTEMPTS", "8"))
RESET_WINDOW_SECONDS = int(os.getenv("RESET_WINDOW_SECONDS", str(15 * 60)))   # 15 min

# In-memory fallback: key -> list[timestamps]. Pruned on every touch.
_MEM_THROTTLE: dict = {}


def _mem_prune(key: str, window: int) -> int:
    now = _time.time()
    ts = [t for t in _MEM_THROTTLE.get(key, []) if now - t < window]
    if ts:
        _MEM_THROTTLE[key] = ts
    else:
        _MEM_THROTTLE.pop(key, None)
    return len(ts)


async def _attempt_count(key: str, window: int) -> int:
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if redis:
        try:
            return int(await redis.get(key) or 0)
        except Exception:
            pass
    return _mem_prune(key, window)


async def _attempt_incr(key: str, window: int) -> int:
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if redis:
        try:
            v = await redis.incr(key)
            await redis.expire(key, window)
            return int(v)
        except Exception:
            pass
    _mem_prune(key, window)
    _MEM_THROTTLE.setdefault(key, []).append(_time.time())
    return len(_MEM_THROTTLE[key])


async def _attempt_clear(keys) -> None:
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    if redis:
        try:
            for k in keys:
                await redis.delete(k)
            return
        except Exception:
            pass
    for k in keys:
        _MEM_THROTTLE.pop(k, None)


def _login_keys(ip: str, email: str):
    keys = []
    if ip:
        keys.append(f"ts:throttle:login:ip:{ip}")
    if email:
        keys.append(f"ts:throttle:login:acct:{email.lower()}")
    return keys


async def check_login_throttle(ip: str = "", email: str = "") -> None:
    """Raise HTTPException(429) if this IP OR account has too many recent failed
    logins. Call BEFORE verifying the password.

    Signature: check_login_throttle(ip: str = "", email: str = "") -> None
    Wire in main.py's /auth/login BEFORE login_user, OR rely on login_user which
    already calls it (per-account throttling works even when ip is "")."""
    from fastapi import HTTPException
    for key in _login_keys(ip, email):
        if await _attempt_count(key, LOGIN_WINDOW_SECONDS) >= LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=429,
                detail="Prea multe încercări de autentificare. "
                       "Încearcă din nou peste câteva minute.")


async def record_login_failure(ip: str = "", email: str = "") -> None:
    """Record ONE failed login against both the IP and account counters.
    Call after a bad email/password. Signature:
    record_login_failure(ip: str = "", email: str = "") -> None"""
    for key in _login_keys(ip, email):
        await _attempt_incr(key, LOGIN_WINDOW_SECONDS)


async def clear_login_attempts(ip: str = "", email: str = "") -> None:
    """Reset the failed-login counters after a SUCCESSFUL login. Signature:
    clear_login_attempts(ip: str = "", email: str = "") -> None"""
    await _attempt_clear(_login_keys(ip, email))


def _reset_keys(ip: str, ident: str):
    keys = []
    if ip:
        keys.append(f"ts:throttle:reset:ip:{ip}")
    if ident:
        keys.append(f"ts:throttle:reset:id:{ident}")
    return keys


async def check_reset_throttle(ip: str = "", ident: str = "") -> None:
    """Raise HTTPException(429) if this IP OR identifier (email for forgot,
    token for reset) has too many recent password-reset attempts. Call BEFORE
    doing the work. Signature: check_reset_throttle(ip: str = "", ident: str = "") -> None"""
    from fastapi import HTTPException
    for key in _reset_keys(ip, ident):
        if await _attempt_count(key, RESET_WINDOW_SECONDS) >= RESET_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=429,
                detail="Prea multe cereri de resetare a parolei. "
                       "Încearcă din nou peste câteva minute.")


async def record_reset_attempt(ip: str = "", ident: str = "") -> None:
    """Record ONE password-reset attempt against both counters. Signature:
    record_reset_attempt(ip: str = "", ident: str = "") -> None"""
    for key in _reset_keys(ip, ident):
        await _attempt_incr(key, RESET_WINDOW_SECONDS)