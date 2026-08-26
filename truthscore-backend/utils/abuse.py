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


async def turnstile_verify(token: str, remoteip: str = "") -> bool:
    """
    Server-side verification of a Turnstile widget token.
    Returns True when: secret not configured (dev mode) OR token valid.
    """
    if not TURNSTILE_SECRET:
        return True   # local/dev — enforcement off
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


# ── Anonymous try-before-signup quota (per IP) ──────────────────
# Curious visitors get a few free verifications WITHOUT an account —
# the taste that drives signups. Requires Redis; without it (local dev)
# anonymous checks are simply allowed.
ANON_DAILY_CAP = int(os.getenv("ANON_DAILY_CAP", "3"))


async def anon_ip_check(ip: str):
    """Returns (allowed, info_dict) for an anonymous visitor."""
    from utils.redis_client import get_async_redis
    redis = get_async_redis()
    base = {"plan": "anonymous", "used": 0, "limit": ANON_DAILY_CAP}
    if not redis or not ip:
        info = dict(base); info["allowed"] = True
        info["note"] = "no-redis dev mode — anonymous not counted"
        return True, info
    day = _utc_day()
    key = f"ts:anon:{ip}:{day}"
    try:
        used = await redis.incr(key)
        await redis.expire(key, 172800)
        allowed = used <= ANON_DAILY_CAP
        info = dict(base); info.update({"used": used, "allowed": allowed})
        return allowed, info
    except Exception:
        info = dict(base); info["allowed"] = True
        return True, info


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