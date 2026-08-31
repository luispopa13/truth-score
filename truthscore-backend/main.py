"""
TruthScore v12 -- Main Entry Point
===================================
Thin entry point. All logic lives in submodules.
Run: uvicorn main:app --reload
"""
from config import *
from models import *
from utils.cache import cache, clear_all_caches
from calibration.ece import record_feedback, record_feedback_durable, calibration_report

# Pipeline
from pipeline.verify  import verify_claim
from pipeline.aggregate import aggregate_score, sub_claim_weight
from pipeline.helpers import split_claims

# User case study logging (MSc thesis evaluation data collection) -- new, self-contained
import asyncio
import time
import json
import secrets as _secrets
from datetime import datetime, timezone
from typing import Optional as _Optional
from fastapi import Response, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from pipeline.case_study import log_interaction
from pipeline.verdict_store import save_verdict, load_verdict

# Auth
try:
    from auth import (
        register_user, login_user, get_current_user, require_user,
        check_rate_limit, get_user_out, upgrade_user_plan,
        UserRegister, UserLogin, UserOut, PLANS, create_token,
        google_auth, google_callback, google_exchange, AUTH_AVAILABLE,
        logout_user, security as _auth_security, get_db,
    )
except ImportError as e:
    print(f"[WARN] Auth not available: {e}")
    AUTH_AVAILABLE = False
    def get_db(): raise HTTPException(503, "Database not configured")
    async def register_user(*a, **k): raise HTTPException(503, "Auth not configured")
    async def login_user(*a, **k):    raise HTTPException(503, "Auth not configured")
    async def get_current_user(**k): return None
    async def require_user(**k):     raise HTTPException(401, "Auth not configured")
    async def check_rate_limit(u,c): return {"allowed":True,"used":0,"limit":10,"plan":"free"}
    async def get_user_out(u):       return {}
    async def upgrade_user_plan(*a,**k): pass
    async def create_token(*a,**k): return ""
    async def google_auth(*a,**k): raise HTTPException(503, "Google auth not configured")
    async def google_callback(*a,**k): raise HTTPException(503, "Google auth not configured")
    async def google_exchange(*a,**k): raise HTTPException(503, "Google auth not configured")
    async def logout_user(*a,**k): return {"status": "ok"}
    _auth_security = None

# Payments
try:
    from api.payments import create_checkout, stripe_webhook, customer_portal
except ImportError:
    async def create_checkout(*a,**k): raise HTTPException(503, "Payments not configured")
    async def stripe_webhook(*a,**k):  return {"status": "ignored"}
    async def customer_portal(*a,**k): raise HTTPException(503, "Payments not configured")

# Batch + PDF
try:
    from api.batch import batch_verify, verify_and_pdf
except ImportError:
    async def batch_verify(*a,**k):   raise HTTPException(503, "Batch not configured")
    async def verify_and_pdf(*a,**k): raise HTTPException(503, "PDF not configured")

# Widget
try:
    from api.widget import widget_script
except ImportError:
    async def widget_script(*a,**k): return ""


# ── FastAPI app ───────────────────────────────────────────
app = FastAPI(
    title       = "TruthScore API",
    description = "Evidence-first AI fact-checking pipeline",
    version     = "12.0",
)

# CORS: read allow-list from env (default: localhost dev ports). Extension
# origins (chrome-extension://<id>, moz-extension://<id>) can't be enumerated
# ahead of time and Starlette ignores "*" wildcards inside allow_origins, so
# they're matched with a regex instead.
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS","").split(",") if o.strip()]
if not _origins:
    _origins = [
        "http://localhost:3000", "http://localhost:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins       = _origins,
    allow_origin_regex  = r"^(chrome-extension|moz-extension)://.*$",
    allow_methods       = ["*"],
    allow_headers       = ["*"],
    allow_credentials   = True,
    # Extension JS must be able to READ these custom response headers
    expose_headers    = [
        "X-TruthScore-Interaction-Id",
        "X-TruthScore-Verdict-Id",
        "X-TruthScore-Show-Ads",
        "X-TruthScore-Quota-Left",
        "X-TruthScore-Truncated",
    ],
)


# ── Observability: structured logging + correlation-ID middleware ────
try:
    from utils.observability import (
        setup_logging, request_id_ctx, METRICS as _METRICS,
    )
    setup_logging()
    _OBS_AVAILABLE = True
except Exception as _e:
    print(f"[WARN] Observability setup skipped: {_e}")
    _OBS_AVAILABLE = False

# Readiness gate: flipped True once startup warmup finishes. Load balancers hit
# /ready and hold traffic (503) until the process can actually serve.
_READY = False


# Security headers: applied to every response. X-Frame-Options + frame-ancestors
# stop clickjacking (the dashboard is never meant to be iframed); nosniff blocks
# MIME-confusion; Referrer-Policy avoids leaking full URLs cross-origin. The CSP
# is REPORT-ONLY for now (it won't break anything) because the dashboard/privacy
# pages use inline <style>/<script> and load AdSense + Google fonts — we ship it
# observe-only first, then flip to enforcing once violations are clean.
_CSP_REPORT_ONLY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://pagead2.googlesyndication.com "
    "https://www.googletagservices.com https://accounts.google.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "connect-src 'self' https:; "
    "frame-src https://accounts.google.com https://googleads.g.doubleclick.net; "
    "frame-ancestors 'none'; base-uri 'self'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Stamp defensive headers on every response (clickjacking, MIME-sniffing,
    referrer leakage). CSP ships report-only until inline usage is cleaned up."""
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    response.headers.setdefault("Content-Security-Policy-Report-Only", _CSP_REPORT_ONLY)
    return response


@app.middleware("http")
async def _correlation_and_metrics(request: Request, call_next):
    """Assign/propagate a correlation id (X-Request-ID), time the request, feed
    the metrics counters, and echo the id back so clients/logs can join traces."""
    import time as _t, uuid as _uuid
    if not _OBS_AVAILABLE:
        return await call_next(request)
    rid = request.headers.get("X-Request-ID") or _uuid.uuid4().hex[:16]
    token = request_id_ctx.set(rid)
    _METRICS.start()
    t0 = _t.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        _METRICS.finish(status, (_t.time() - t0) * 1000)
        request_id_ctx.reset(token)


@app.on_event("startup")
async def _warmup_models():
    """
    Pre-loads the embedding + cross-encoder models into memory at server
    boot, instead of lazily on the first /verify call. This moves the
    one-time ~30-40s model-loading cost to server startup (visible once
    in the console) so the first user request after a --reload restart
    isn't slow.
    """
    try:
        from pipeline.ranking import _get_embed_model, _get_cross_encoder
        print("[STARTUP] Warming up local models...")
        _get_embed_model(multilingual=False)
        _get_embed_model(multilingual=True)
        _get_cross_encoder()
        print("[STARTUP] Models ready.")
    except Exception as e:
        print(f"[STARTUP] Model warmup skipped (non-fatal): {e}")

    # Verify Redis is actually reachable (from_url is lazy — a down server would
    # otherwise masquerade as "connected" and cost a connect-timeout per call).
    try:
        from utils.redis_client import verify_async_redis
        ok = await verify_async_redis()
        print("[STARTUP] Redis reachable." if ok else
              "[STARTUP] Redis not reachable — single-instance mode.")
    except Exception as e:
        print(f"[STARTUP] Redis check skipped (non-fatal): {e}")

    # Capability report — log which optional integrations are configured so a
    # degraded subsystem is visible at boot, not discovered mid-incident.
    try:
        from utils.health import log_startup_health
        log_startup_health()
    except Exception as e:
        print(f"[STARTUP] Health report skipped (non-fatal): {e}")

    # Seed the daily challenge pool + wire source-credibility DB (best-effort).
    try:
        from auth import get_db
        db = get_db()
        from api.challenge import seed_challenges
        await seed_challenges(db)
        from pipeline.verdict_store import set_credibility_db
        set_credibility_db(db)
        print("[STARTUP] Challenges seeded + credibility DB wired.")
    except Exception as e:
        print(f"[STARTUP] Challenge/credibility init skipped (non-fatal): {e}")

    # Flip the readiness gate — the process can now serve traffic.
    global _READY
    _READY = True
    print("[STARTUP] Ready to serve.")
    # Periodic jobs (digests, news scan) run on a SINGLE leader-elected worker so
    # they don't fire once per gunicorn worker. Metrics flushing is per-process
    # (counters are per-worker), so it runs everywhere.
    asyncio.create_task(_scheduler_loop())
    asyncio.create_task(_metrics_flush_loop())


async def _scheduler_loop():
    """Single leader-elected loop for all periodic jobs.

    Only ONE process across the fleet runs the body (Redis leader lock, with a
    worker-0 fallback when Redis is down), so digests/scans never fire N× with N
    gunicorn workers. Renews the lock each tick and steps down if leadership is
    lost. Fires: daily email digest (~08 UTC), weekly "lies of the week" digest
    (Sundays ~09 UTC), daily news scan (~06 UTC) + monitor checks.
    """
    import datetime as _dt
    from utils.redis_client import should_run_scheduler, renew_scheduler_lock
    if not await should_run_scheduler():
        print("[SCHED] Not the scheduler leader — periodic jobs disabled here.")
        return
    print("[SCHED] Elected scheduler leader — periodic jobs active.")
    sent_daily: set = set()
    sent_weekly: set = set()
    scanned_days: set = set()
    while True:
        try:
            # Tick well under the default 300s lock TTL so renewal never lapses.
            await asyncio.sleep(240)
            if not await renew_scheduler_lock():
                print("[SCHED] Lost scheduler lock — stepping down.")
                return
            now = _dt.datetime.now(_dt.timezone.utc)
            db = get_db()
            day_key = now.strftime("%Y-%m-%d")
            iso = now.isocalendar()
            week_key = f"{iso[0]}-W{iso[1]}"

            # Daily email digest ~08:00 UTC
            if now.hour == 8 and day_key not in sent_daily:
                from email_digest import run_digest
                await run_digest(db)
                sent_daily.add(day_key)
                print(f"[DIGEST] Daily digest sent {day_key}")

            # Weekly "lies of the week" digest — Sundays ~09:00 UTC
            if now.weekday() == 6 and now.hour == 9 and week_key not in sent_weekly:
                from email_digest import run_weekly_lies_digest
                await run_weekly_lies_digest(db)
                sent_weekly.add(week_key)
                print(f"[DIGEST] Weekly lies digest sent {week_key}")

            # Daily news scan ~06:00 UTC, then fan out monitor alerts
            if now.hour == 6 and day_key not in scanned_days:
                from news_scanner import run_scan
                result = await run_scan(db)
                scanned_days.add(day_key)
                print(f"[SCANNER] Daily scan complete: {result}")
                await _run_monitor_checks(db)
        except Exception as e:
            print(f"[SCHED] loop error: {e}")


async def _metrics_flush_loop():
    """Per-worker loop: flush in-memory metrics to their sink periodically.
    NOT leader-gated — counters accumulate per process, so every worker flushes
    its own."""
    from utils.metrics import flush_metrics
    while True:
        try:
            await asyncio.sleep(600)
            await flush_metrics()
        except Exception as e:
            print(f"[METRICS] flush error: {e}")


async def _run_monitor_checks(db):
    """Send alert emails to users whose monitors match today's new claims."""
    try:
        today = __import__('datetime').date.today().isoformat()
        new_claims = await db.daily_checks.find_one({"date": today})
        if not new_claims:
            return
        claims_today = new_claims.get("results", [])
        monitors = await db.monitors.find({"active": True}).to_list(500)
        for mon in monitors:
            kw = (mon.get("keyword") or "").lower()
            if not kw:
                continue
            matched = [c for c in claims_today if kw in (c.get("claim") or "").lower()]
            if not matched:
                continue
            from utils.mailer import send_email
            user = await db.users.find_one({"_id": mon["user_id"]})
            email = (user or {}).get("email")
            if not email:
                continue
            items = "".join(f"<li><b>{c.get('verdict','?')}</b>: {c.get('claim','')[:200]}</li>" for c in matched[:5])
            html = f"<h2>🚨 TruthScore Monitor Alert: «{mon.get('name','Monitor')}»</h2><p>S-au găsit <b>{len(matched)}</b> claims noi care conțin «{kw}» astăzi:</p><ul>{items}</ul><p><a href='{PUBLIC_BASE_URL}/trending'>Vezi toate</a></p>"
            await send_email(email, f"[TruthScore Monitor] {len(matched)} claims noi: «{kw}»", html)
    except Exception as e:
        print(f"[MONITOR] check error: {e}")
    """Cleanly close the shared retrieval HTTP connection pool on shutdown."""
    try:
        from pipeline.retrieval import close_shared_http_client
        await close_shared_http_client()
    except Exception as e:
        print(f"[SHUTDOWN] HTTP pool close skipped (non-fatal): {e}")


# ── Core endpoints ────────────────────────────────────────

# Admin gate for ops/business-intelligence endpoints. An account is admin only
# if its email is listed in ADMIN_EMAILS (comma-separated). Empty list ⇒ no one
# is admin, so these endpoints stay locked until the owner opts in explicitly.
_ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


async def require_admin(user=Depends(require_user)):
    email = (user.get("email") or "").lower() if user else ""
    if not _ADMIN_EMAILS or email not in _ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# X-Forwarded-For is set by the client and trivially spoofable, so honoring it
# blindly lets an attacker rotate the header to dodge the anonymous per-IP quota.
# Trust it ONLY when TRUST_PROXY says we sit behind a proxy/LB that overwrites
# it; otherwise fall back to the socket peer, which can't be forged.
#
# When trusted, take the Nth-from-RIGHT entry, not the leftmost. Each proxy in
# the chain APPENDS the address it saw, so the rightmost entries are the ones our
# own infrastructure added and can be trusted; the leftmost are client-supplied
# and spoofable. TRUSTED_PROXY_HOPS = how many proxies sit in front of us
# (default 1): with one hop we want the last entry, with two we want the
# second-to-last, etc. This stops "X-Forwarded-For: <spoofed>, <real>" evasion.
_TRUST_PROXY = os.getenv("TRUST_PROXY", "").lower() in ("1", "true", "yes")
_TRUSTED_PROXY_HOPS = max(1, int(os.getenv("TRUSTED_PROXY_HOPS", "1")))


def _client_ip(request: Request) -> str:
    if _TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                # Nth-from-right; clamp to the leftmost if the chain is shorter
                # than expected (never index past the start of the list).
                idx = min(_TRUSTED_PROXY_HOPS, len(parts))
                return parts[-idx]
    return request.client.host if request.client else ""


def _client_fp(request: Request) -> str:
    """Browser fingerprint sent by the frontend (X-Browser-Fp header).
    Used to track anonymous quota by device across incognito sessions.
    Truncated to 64 chars — it's a hex digest, only first 32 chars matter.
    """
    return (request.headers.get("x-browser-fp") or "")[:64]


def _is_paid(user: dict | None) -> bool:
    """Paid = a signed-in user on any plan other than the free tier."""
    return bool(user) and user.get("plan", "free") != "free"


async def enforce_quota(user: dict | None, text: str, client_ip: str, fp: str = "") -> dict | None:
    """Check the daily quota BEFORE any expensive LLM work, with an asymmetric
    failure policy:

      • Quota exceeded            → 429 (both free and paid).
      • Limiter itself errors     → FAIL CLOSED for anonymous/free users
                                    (503), FAIL OPEN for paid users (return
                                    None so a Redis hiccup never blocks a
                                    paying customer).

    The old behaviour was fail-open for everyone (`except Exception: pass`),
    which let anyone bypass the free/anon limit — and rack up LLM cost — simply
    by making the limiter throw. Free/anon abuse is the cost risk, so those
    users are the ones we stop when we can't prove they're within quota.
    """
    from http import HTTPStatus
    try:
        info = await check_rate_limit(user, text, client_ip=client_ip, fp=fp)
    except Exception as e:
        if _is_paid(user):
            print(f"[RATE-LIMIT] limiter error, failing OPEN for paid user: {e}")
            return None
        print(f"[RATE-LIMIT] limiter error, failing CLOSED for free/anon: {e}")
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Serviciul e temporar indisponibil. Încearcă din nou în câteva momente.",
        )
    if info and not info.get("allowed"):
        if info.get("plan") == "anonymous":
            _cap = info.get("limit", 5)
            detail = (f"Ai folosit cele {_cap} verificari gratuite anonime. "
                      "Creeaza un cont gratuit pentru 10/zi + bonusuri.")
        else:
            detail = (f"Limita zilnica de {info.get('limit')} verificari atinsa. "
                      "Upgrade la Pro pentru mai mult: /?pricing=1")
        raise HTTPException(status_code=HTTPStatus.TOO_MANY_REQUESTS, detail=detail)
    return info

def _set_ads_and_quota_headers(response, user, quota_left=None):
    """
    Stamp the shared monetization headers on a response.
      X-TruthScore-Show-Ads : "1" only for free/anonymous users when AdSense
                              is configured and ADS_ENABLED is on; paid = "0".
      X-TruthScore-Quota-Left: remaining checks today (omitted when unknown).
    Used identically by /verify and /analyze-text so the ad + quota contract
    can never diverge between the two endpoints.
    """
    ads_on = (os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
              and bool(os.getenv("ADSENSE_CLIENT", "").strip()))
    # House ads (internal Upgrade-to-Pro promo) have no external dependency, so
    # free users should see the ad zone even before AdSense is configured; the
    # frontend picks live-vs-house. Keep it off for paid users.
    house_on = os.getenv("HOUSE_ADS_ENABLED", "true").lower() in ("1", "true", "yes")
    is_free = (not user) or user.get("plan", "free") == "free"
    response.headers["X-TruthScore-Show-Ads"] = "1" if ((ads_on or house_on) and is_free) else "0"
    if quota_left is not None:
        response.headers["X-TruthScore-Quota-Left"] = str(max(0, int(quota_left)))


@app.post("/verify", response_model=VerifyResponse)
async def verify(req: VerifyRequest, response: Response,
                 request: Request,
                 user: dict = Depends(get_current_user)):
    """
    Verify a claim.  Auth required for anonymous-free usage.
    Cache hits are FREE and don't count toward the rate limit.
    """
    # ── Rate limiting ──────────────────────────────────────
    # Check quota BEFORE the expensive LLM work.  Redis-backed, atomic.
    # Fails CLOSED for free/anon and OPEN for paid (see enforce_quota).
    client_ip = _client_ip(request)
    rate_info = await enforce_quota(user, req.text, client_ip=client_ip, fp=_client_fp(request))

    start = time.perf_counter()
    # Heavy-day paid users past their eco threshold get cheap-model routing
    # and skip the paid-search / deep-decomposition steps (margin protection).
    eco = bool(rate_info.get("eco")) if rate_info else False
    result = await verify_claim(req, eco=eco)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)

    # ── Post-pipeline bookkeeping — all run concurrently, none block the response ──
    async def _do_log():
        try:
            iid = await log_interaction({
                "claim":               getattr(result, "claim", req.text)[:500],
                "topic":               getattr(result, "topic", None),
                "verdict":             getattr(result, "verdict", None),
                "score":               getattr(result, "score", None),
                "confidence":          getattr(result, "confidence", None),
                "evidence_count":      getattr(result, "evidence_count", None),
                "supporting_count":    len(getattr(result, "supporting", []) or []),
                "contradicting_count": len(getattr(result, "contradicting", []) or []),
                "neutral_count":       len(getattr(result, "neutral_sources", []) or []),
                "models_used":         getattr(result, "models_used", None),
                "cached":              getattr(result, "cached", False),
                "duration_ms":         duration_ms,
                "user_id":             user.get("id") if user else None,
                "user_plan":           (user.get("plan") if user else "anonymous"),
            })
            response.headers["X-TruthScore-Interaction-Id"] = iid
        except Exception as e:
            print(f"[CASE-STUDY] Logging skipped (non-fatal): {e}")

    async def _do_save():
        try:
            _uid = (user.get("id") or "") if user else ""
            _vid = await save_verdict(result.model_dump(), user_id=_uid)
            if _vid:
                response.headers["X-TruthScore-Verdict-Id"] = _vid
        except Exception as e:
            print(f"[VERDICT-STORE] save skipped (non-fatal): {e}")

    async def _do_trending():
        try:
            from auth import get_db
            from pipeline.trending import record_check
            await record_check(get_db(), result.claim, result.verdict,
                               result.score, result.topic)
        except Exception as e:
            print(f"[TRENDING] record skipped (non-fatal): {e}")

    async def _do_push():
        try:
            from auth import get_db
            from api.push_notifications import notify_claim_watchers
            _slug = getattr(result, '_claimSlug', '') or ''
            await notify_claim_watchers(get_db(), result.claim, result.verdict, _slug)
        except Exception as e:
            print(f"[PUSH] notify watchers skipped: {e}")

    async def _do_upsert():
        try:
            from auth import get_db
            from pipeline.public_claims import upsert_public_claim
            all_src = [
                *(result.supporting or []),
                *(result.contradicting or []),
                *(result.neutral_sources or []),
            ]
            _slug = await upsert_public_claim(
                get_db(),
                claim=result.claim,
                verdict=result.verdict,
                score=result.score,
                sources=[s.model_dump() if hasattr(s, "model_dump") else s for s in all_src],
                explanation=getattr(result, "aggregate_reason", "") or "",
                topic=result.topic or "",
            )
            if _slug:
                response.headers["X-TruthScore-Claim-Slug"] = _slug
        except Exception as e:
            print(f"[PUBLIC-CLAIMS] upsert skipped (non-fatal): {e}")

    # Run all bookkeeping in parallel — headers are set before we return
    async def _do_streak():
        try:
            if user and user.get("id"):
                from auth import touch_streak
                st = await touch_streak(user["id"])
                response.headers["X-TruthScore-Streak"] = str(st.get("streak", 0))
        except Exception as e:
            print(f"[STREAK] update skipped (non-fatal): {e}")

    async def _do_timeline():
        # Append a snapshot to the temporal-drift timeline on EVERY verify so
        # `/claims/timeline` actually accumulates history over time (previously
        # only the 30-day admin scan wrote snapshots). record_verdict_snapshot
        # dedups when the verdict+score are unchanged, so re-checks don't spam.
        try:
            from auth import get_db
            from pipeline.temporal_drift import record_verdict_snapshot
            src_urls = [s.url for s in (result.supporting or [])[:5] if getattr(s, "url", "")]
            await record_verdict_snapshot(
                get_db(), result.claim, result.verdict, result.score,
                getattr(result, "explanation", "") or "", src_urls,
            )
        except Exception as e:
            print(f"[TIMELINE] snapshot skipped (non-fatal): {e}")

    async def _do_entities():
        # Build/refresh entity reliability profiles from this claim. Entity
        # extraction spends one free-tier LLM call, so it's skipped on eco-mode
        # heavy-day requests where we're actively shedding cost. Runs in the
        # background gather, so it never adds latency to the response.
        if eco:
            return
        try:
            from auth import get_db
            from pipeline.entity_memory import update_entity_profiles
            is_misleading = "MISLEAD" in (result.verdict or "").upper()
            await update_entity_profiles(
                get_db(), result.claim, result.verdict, result.score,
                is_misleading=is_misleading,
            )
        except Exception as e:
            print(f"[ENTITY] profile update skipped (non-fatal): {e}")

    await asyncio.gather(
        _do_log(), _do_save(), _do_trending(), _do_push(), _do_upsert(), _do_streak(),
        _do_timeline(), _do_entities(),
        return_exceptions=True,
    )

    # Cheap (LLM-free) entity-history enrichment — surfaces any named entity in
    # this claim that already has an accumulated reliability profile. Pure regex
    # + indexed Mongo lookups, safe on the hot path; the UI renders these.
    try:
        from auth import get_db
        from pipeline.entity_memory import get_profiles_for_text_cheap
        result.entity_profiles = await get_profiles_for_text_cheap(get_db(), result.claim)
    except Exception as e:
        print(f"[ENTITY] profile surface skipped (non-fatal): {e}")


    # Internal telemetry records model choices before this point. Public clients
    # receive no provider/model metadata, which also keeps the API contract clean.
    result.models_used = []

    # ── Ad-support flag (free tier monetization) ─────────────────
    # Extension/dashboard reads this header and renders the sponsor slot.
    # Paid plans never see ads.
    try:
        quota_left = None
        if rate_info:
            quota_left = int(rate_info.get("limit", 0)) - int(rate_info.get("used", 0))
        _set_ads_and_quota_headers(response, user, quota_left)
    except Exception:
        pass

    return result



@app.post("/verify-stream")
async def verify_stream(req: VerifyRequest, request: Request,
                        user: dict = Depends(get_current_user)):
    """Server-Sent Events variant of /verify.

    Emits real-time progress stages while the pipeline runs, then a final
    `result` event carrying the full VerifyResponse. Quota is enforced up-front
    so a rejection is a normal HTTP 429 (not an SSE frame). Clients that can't
    read SSE should keep using POST /verify — this is purely a UX enhancement.
    """
    import asyncio as _asyncio, json as _json
    from fastapi.responses import StreamingResponse

    client_ip = _client_ip(request)
    rate_info = await enforce_quota(user, req.text, client_ip=client_ip, fp=_client_fp(request))
    eco = bool(rate_info.get("eco")) if rate_info else False

    # Compute monetization signals now (can't set headers once streaming starts).
    ads_on = (os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
              and bool(os.getenv("ADSENSE_CLIENT", "").strip()))
    house_on = os.getenv("HOUSE_ADS_ENABLED", "true").lower() in ("1", "true", "yes")
    is_free = (not user) or user.get("plan", "free") == "free"
    show_ads = bool((ads_on or house_on) and is_free)
    quota_left = None
    if rate_info:
        try:
            quota_left = max(0, int(rate_info.get("limit", 0)) - int(rate_info.get("used", 0)))
        except Exception:
            quota_left = None

    async def _sse():
        def _evt(obj):
            return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"
        start = time.perf_counter()
        partial_q = _asyncio.Queue()
        task = _asyncio.create_task(verify_claim(req, eco=eco, on_partial=partial_q))
        try:
            yield _evt({"stage": "classify"})
            emitted_partial = False
            while not task.done():
                done, _pending = await _asyncio.wait({task}, timeout=1.0)
                if not done:
                    # Check for partial result from Path A+B
                    if not emitted_partial:
                        try:
                            partial = partial_q.get_nowait()
                            p_payload = partial.model_dump() if hasattr(partial, "model_dump") else dict(partial)
                            p_payload["show_ads"] = show_ads
                            if quota_left is not None:
                                p_payload["quota_left"] = quota_left
                            yield _evt({"event": "partial", "data": p_payload})
                            emitted_partial = True
                        except _asyncio.QueueEmpty:
                            yield _evt({"heartbeat": True})
                    else:
                        yield _evt({"heartbeat": True})
            try:
                result = await task
            except HTTPException as he:
                yield _evt({"event": "error", "detail": str(he.detail), "status": he.status_code}); return
            except Exception as e:
                yield _evt({"event": "error", "detail": str(e)[:200]}); return

            duration_ms = round((time.perf_counter() - start) * 1000, 1)

            async def _log_sse():
                try:
                    return await log_interaction({
                        "claim":               getattr(result, "claim", req.text)[:500],
                        "topic":               getattr(result, "topic", None),
                        "verdict":             getattr(result, "verdict", None),
                        "score":               getattr(result, "score", None),
                        "confidence":          getattr(result, "confidence", None),
                        "evidence_count":      getattr(result, "evidence_count", None),
                        "cached":              getattr(result, "cached", False),
                        "duration_ms":         duration_ms,
                        "user_id":             user.get("id") if user else None,
                        "user_plan":           (user.get("plan") if user else "anonymous"),
                    })
                except Exception as e:
                    print(f"[CASE-STUDY] Logging skipped (non-fatal): {e}")
                    return ""

            async def _save_sse():
                try:
                    _uid = (user.get("id") or "") if user else ""
                    return await save_verdict(result.model_dump() if hasattr(result, "model_dump") else dict(result), user_id=_uid)
                except Exception as e:
                    print(f"[VERDICT-STORE] save skipped (non-fatal): {e}")
                    return None

            interaction_id, _vid = await _asyncio.gather(_log_sse(), _save_sse(), return_exceptions=True)
            if isinstance(interaction_id, Exception): interaction_id = ""
            if isinstance(_vid, Exception): _vid = None

            result.models_used = []
            payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            payload["latency_ms"] = duration_ms
            payload["partial"] = False
            if interaction_id:
                payload["_interactionId"] = interaction_id
            if _vid:
                payload["_verdictId"] = _vid
            try:
                from pipeline.verdict_store import increment_and_get_check_count
                payload["check_count"] = increment_and_get_check_count(req.text)
            except Exception:
                pass
            payload["show_ads"] = show_ads
            if quota_left is not None:
                payload["quota_left"] = quota_left
            yield _evt({"event": "result", "data": payload})
        finally:
            # If the client disconnected (or the generator was closed) before the
            # pipeline finished, cancel the orphaned compute — otherwise it keeps
            # burning LLM/search cost producing a result nobody will read.
            if not task.done():
                task.cancel()

    return StreamingResponse(_sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.post("/analyze-text", response_model=TextAnalysisResponse)
async def analyze_text(req: VerifyRequest, response: Response,
                       request: Request,
                       user: dict = Depends(get_current_user)):
    """Extract and independently verify every factual claim in a paragraph."""
    return await _analyze_text_core(req.text.strip(), response, request, user)


async def _analyze_text_core(text: str, response: Response, request: Request,
                             user: dict) -> TextAnalysisResponse:
    """Shared engine for paragraph verification: quota → claim split → per-claim
    verify → authority-weighted aggregate. Reused verbatim by /analyze-text
    (typed input) and /verify-image (OCR'd screenshot input) so both paths get
    identical scoring, quota accounting, and response shape."""
    client_ip = _client_ip(request)
    fp = _client_fp(request)

    # Reserve one quota unit before paying for claim extraction.
    # Fails CLOSED for free/anon, OPEN for paid (see enforce_quota).
    quota = await enforce_quota(user, text, client_ip=client_ip, fp=fp)
    # Eco state is a per-user daily flag — capture it from the first check
    # before the per-claim loop below reassigns `quota`.
    eco = bool(quota.get("eco")) if quota else False

    claims = await split_claims(text)
    if not claims:
        raise HTTPException(status_code=422, detail="No verifiable factual claims were found.")

    # Every extracted claim is a real verification and consumes one quota unit.
    allowed_claims = [claims[0]]
    for claim in claims[1:]:
        try:
            q = await check_rate_limit(user, claim, client_ip=client_ip, fp=fp)
        except Exception:
            # Limiter hiccup mid-loop: stop granting further claims rather than
            # silently handing out unmetered verifications.
            break
        if not q.get("allowed"):
            break
        quota = q
        allowed_claims.append(claim)

    if len(allowed_claims) < len(claims):
        response.headers["X-TruthScore-Truncated"] = "1"

    # Each verified claim consumed exactly one quota unit (the initial
    # reservation covers claims[0]). Surfaced for fair-use transparency.
    quota_consumed = len(allowed_claims)
    response.headers["X-TruthScore-Quota-Consumed"] = str(quota_consumed)
    try:
        quota_left = max(0, int(quota.get("limit", 0)) - int(quota.get("used", 0)))
    except Exception:
        quota_left = -1

    semaphore = asyncio.Semaphore(max(1, int(os.getenv("PARAGRAPH_CONCURRENCY", "3"))))

    async def _verify_one(claim: str) -> VerifyResponse:
        async with semaphore:
            result = await verify_claim(VerifyRequest(text=claim), eco=eco)
            result.models_used = []
            return result

    raw_results = await asyncio.gather(
        *[_verify_one(claim) for claim in allowed_claims],
        return_exceptions=True,
    )
    results = [r for r in raw_results if isinstance(r, VerifyResponse)]
    if not results:
        raise HTTPException(status_code=503, detail="The claims could not be verified right now.")

    # Stamp each source with the claim it belongs to (source -> claim mapping)
    # and build sub-claim results so the paragraph score is the authority-
    # weighted aggregate of its claims, not a plain mean. A false claim backed
    # by a fact-checker should pull the paragraph down harder than a shaky
    # "true" backed by a blog lifts it.
    sub_results = []
    for i, r in enumerate(results):
        for s in (r.supporting + r.contradicting + r.neutral_sources):
            s.claim_index = i
        sub_results.append(SubClaimResult(
            claim_index=i,
            claim=r.claim,
            score=r.score,
            verdict=r.verdict,
            confidence=r.confidence,
            explanation=r.explanation or "No detailed explanation available.",
            topic=r.topic,
            supporting=r.supporting,
            contradicting=r.contradicting,
            neutral_sources=r.neutral_sources,
            evidence_count=r.evidence_count,
            weight=sub_claim_weight(r.supporting, r.contradicting,
                                    r.neutral_sources, r.verdict, r.score),
        ))

    verdicts = [r.verdict for r in results]
    true_count = verdicts.count("TRUE")
    false_count = verdicts.count("FALSE")
    uncertain_count = len(results) - true_count - false_count
    mixed = true_count > 0 and false_count > 0

    agg_score, agg_verdict, agg_conf, aggregate_reason = aggregate_score(sub_results)
    score = agg_score
    confidence = agg_conf
    # MIXED stays a distinct UI verdict when claims genuinely disagree; otherwise
    # use the weighted aggregate verdict.
    verdict = "MIXED" if mixed else agg_verdict

    explanation = (
        f"Analyzed {len(results)} factual claim(s): {true_count} supported, "
        f"{false_count} contradicted and {uncertain_count} uncertain."
    )
    if quota_left >= 0:
        explanation += (
            f" Consumed {quota_consumed} verification(s); "
            f"{quota_left} left today.")

    response.headers["X-TruthScore-Quota-Left"] = str(quota_left)
    _set_ads_and_quota_headers(response, user)

    try:
        from pipeline.verdict_store import increment_and_get_check_count
        check_count = increment_and_get_check_count(text)
    except Exception:
        check_count = 0

    return TextAnalysisResponse(
        text=text,
        verdict=verdict,
        score=score,
        confidence=confidence,
        explanation=explanation,
        aggregate_reason=aggregate_reason,
        results=results,
        claim_count=len(results),
        mixed=mixed,
        quota_consumed=quota_consumed,
        quota_left=quota_left,
        check_count=check_count,
    )


@app.post("/verify-image", response_model=TextAnalysisResponse)
async def verify_image(response: Response, request: Request,
                       file: UploadFile = File(...),
                       user: dict = Depends(get_current_user)):
    """Verify the claims in a SCREENSHOT. The frictionless on-ramp: a user
    uploads/pastes an image of a viral post, chat forward, or headline; we OCR
    the text with a vision model, extract the factual claims, and run them
    through the identical paragraph pipeline — same scoring, sources, quota, and
    shareable verdict. The image path validates bytes before touching the model
    (magic-byte sniff + size cap) so we never feed un-sniffed input to Gemini."""
    from pipeline.vision import validate_image, extract_text_from_image, MAX_IMAGE_BYTES

    # Bounded read: stop at the cap + 1 byte so an oversized upload can't exhaust
    # memory before validate_image() rejects it.
    data = await file.read(MAX_IMAGE_BYTES + 1)
    ok, mime, err = validate_image(data or b"", file.content_type or "")
    if not ok:
        raise HTTPException(status_code=422, detail=err)

    text = await extract_text_from_image(data, mime)
    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail="No readable factual claim was found in the image.")

    return await _analyze_text_core(text, response, request, user)


@app.post("/detect-claims")
async def detect_claims(req: ClaimDetectRequest, request: Request,
                        user: dict = Depends(get_current_user)):
    """Claim-splitting preview. Rate-limited because it invokes an LLM call."""
    client_ip = _client_ip(request)
    await enforce_quota(user, req.text, client_ip=client_ip, fp=_client_fp(request))
    claims = await split_claims(req.text)
    return {"claims": claims, "count": len(claims)}


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request,
                   user: dict = Depends(get_current_user)):
    # Rate-limit per IP so the calibration/ECE loop can't be flooded or skewed by
    # one actor. Fails open if Redis is down (see feedback_can_submit).
    from utils.abuse import feedback_can_submit
    if not await feedback_can_submit(_client_ip(request)):
        raise HTTPException(429, "Too many feedback submissions today. Please try again tomorrow.")
    # Resolve either naming convention: the dashboard sends
    # verdict/score/correct, the browser extension sends
    # predicted_verdict/predicted_score/user_says_correct.
    verdict = req.verdict or req.predicted_verdict or "UNCERTAIN"
    score   = req.score if req.score is not None else (
              req.predicted_score if req.predicted_score is not None else 50)
    correct = req.user_says_correct if req.user_says_correct is not None else req.correct

    # Update the in-memory calibration state (used by the ECE curve /
    # weak-domain analysis) AND durably persist the feedback so it survives
    # restarts / cloud redeploys.
    record_feedback(
        claim          = req.claim,
        verdict        = verdict,
        score          = score,
        topic          = req.topic or "general",
        correct        = correct,
        failure_reason = req.failure_reason or "",
        interaction_id = req.interaction_id or "",
    )
    try:
        await record_feedback_durable(
            claim          = req.claim,
            verdict        = verdict,
            score          = score,
            topic          = req.topic or "general",
            correct        = correct,
            failure_reason = req.failure_reason or "",
            interaction_id = req.interaction_id or "",
        )
    except Exception as e:
        print(f"[FEEDBACK] Durable persistence skipped (non-fatal): {e}")

    # ── Gamified bonus: +1 check per unique feedback (free-tier hook).
    # Doubles as calibration-data collection for the ECE loop.
    bonus_info = {"granted": False}
    if user and user.get("id") and req.claim:
        try:
            from utils.abuse import grant_feedback_bonus
            bonus_info = await grant_feedback_bonus(str(user["id"]), req.claim)
        except Exception as e:
            print(f"[FEEDBACK-BONUS] skipped: {e}")

    resp = {"status": "recorded"}
    if user:
        resp.update({
            "bonus": bonus_info,
            "message": ("+1 verificare bonus! 🎉" if bonus_info.get("granted")
                        else None),
        })
    return resp


# ── Ops (admin-gated) ────────────────────────────────────────

@app.post("/clear-cache")
async def clear_cache_endpoint(user=Depends(require_admin)):
    """Flush all cached verify results. Admin-only: a public flush would let
    anyone wipe the shared semantic cache and force costly LLM recomputation
    for every user (a cost + latency attack)."""
    result = clear_all_caches()
    print(f"[CACHE] Cleared {result['cleared']} entries from {result['cache_dir']}")
    return {
        "status":  "cleared",
        "entries": result["cleared"],
        "dir":     result["cache_dir"],
    }



@app.get("/health")
async def health():
    import config as _cfg
    groq_key = os.getenv("GROQ_API_KEY", "")
    try:
        from utils.health import capability_report
        capabilities = capability_report()
    except Exception:
        capabilities = {}
    return {
        "status":  "ok",
        "version": "12.0",
        "gemini":  f"set ({GEMINI_MODEL})" if GEMINI_API_KEY else "MISSING",
        "groq":    "set" if groq_key else "missing",
        "tavily":  "set" if TAVILY_API_KEY else "missing",
        "auth":    "available" if AUTH_AVAILABLE else "stub",
        "cache":   f"{len(cache)} entries",
        "capabilities": capabilities,
        "features": {
            "hyde":             True,
            "cross_encoder":    True,
            "path_b":           True,
            "factscore":        True,
            "averitec":         True,
            "wikidata_sparql":  True,
            "targeted_queries": True,
            "search_grounding": bool(GEMINI_API_KEY and getattr(_cfg,'_SEARCH_TOOL',None)),
            "multi_model":      bool(groq_key),
        },
    }


@app.get("/live", include_in_schema=False)
async def liveness():
    """Liveness probe — the process is up and the event loop responds.
    Always 200 unless the worker is wedged (then it won't answer at all)."""
    return {"status": "alive"}


@app.get("/ready", include_in_schema=False)
async def readiness():
    """Readiness probe — 200 only after startup warmup finished. Load balancers
    hold traffic (503) until then so the first real request isn't the one that
    pays the 30-40s model-load cost."""
    if not _READY:
        raise HTTPException(503, "warming up")
    return {"status": "ready"}


@app.get("/metrics")
async def metrics(user=Depends(require_admin)):
    """In-process request metrics (admin-only). Uptime, request/error counts,
    in-flight, average latency, and a per-status-class breakdown."""
    if not _OBS_AVAILABLE:
        return {"error": "observability not available"}
    return _METRICS.snapshot()


@app.get("/")
async def root():
    from fastapi.responses import HTMLResponse
    from pathlib import Path
    try:
        # Anchor to this file's directory — not the CWD — so serving works no
        # matter where uvicorn was launched from (e.g. a systemd unit, Docker).
        return HTMLResponse((Path(__file__).parent / "Dashboard.html").read_text(encoding="utf-8"))
    except Exception:
        return HTMLResponse("<h1>TruthScore v12</h1><p>API running.</p>")


def _esc(s: str) -> str:
    """Minimal HTML-attribute/text escaping for OG tags and page text."""
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


_VERDICT_PAGE_COLORS = {
    "TRUE": "#2ecc71", "FALSE": "#e74c3c", "UNCERTAIN": "#f1c40f",
    "MISLEADING": "#e67e22", "UNVERIFIABLE": "#95a5a6",
}


@app.get("/v/{vid}/card.png", include_in_schema=False)
async def verdict_card(vid: str):
    """Open-Graph social card (1200×630 PNG) for a shared verdict permalink."""
    from fastapi.responses import Response as _Resp
    rec = await load_verdict(vid)
    if not rec:
        raise HTTPException(404, "Verdict not found")
    try:
        from pipeline.social_card import render_card
        png = render_card(vid, rec.get("verdict", "UNCERTAIN"),
                          int(rec.get("score", 50)), rec.get("claim", ""))
    except Exception as e:
        print(f"[VERDICT-CARD] render failed: {e}")
        raise HTTPException(500, "card render failed")
    return _Resp(content=png, media_type="image/png",
                 headers={"Cache-Control": "public, max-age=86400"})


@app.get("/v/{vid}", include_in_schema=False)
async def verdict_page(vid: str, request: Request):
    """Permanent, crawlable, frozen snapshot of a verification at a stable URL.

    This is the moat: a shareable citation that unfurls (OG card) in chats and
    social, unlike an ephemeral chatbot answer. The page renders server-side so
    crawlers see real content + meta tags without executing JS, and links back to
    the live dashboard for a fresh re-check.
    """
    from fastapi.responses import HTMLResponse
    rec = await load_verdict(vid)
    if not rec:
        return HTMLResponse(
            "<h1>Verdict not found</h1><p>This link may have expired or is "
            f"invalid. <a href='/'>Check a claim on TruthScore</a>.</p>",
            status_code=404)

    payload = rec.get("payload", {}) or {}
    claim = rec.get("claim", "") or payload.get("claim", "")
    verdict = (rec.get("verdict", "UNCERTAIN") or "UNCERTAIN").upper()
    score = int(rec.get("score", 50))
    explanation = payload.get("explanation", "") or payload.get("aggregate_reason", "")
    color = _VERDICT_PAGE_COLORS.get(verdict, "#95a5a6")

    base = str(request.base_url).rstrip("/")
    page_url = f"{base}/v/{vid}"
    card_url = f"{page_url}/card.png"
    title = f"{verdict} ({score}/100) — TruthScore"
    desc = (explanation or claim)[:280]

    # Source lists (frozen snapshot).
    def _src_items(items):
        out = []
        for s in (items or [])[:12]:
            u = _esc(s.get("url", ""))
            t = _esc(s.get("title") or s.get("publisher") or s.get("url", ""))
            pub = _esc(s.get("publisher", ""))
            if not u:
                continue
            out.append(f'<li><a href="{u}" target="_blank" rel="noopener nofollow">{t}</a>'
                       + (f' <span class="pub">{pub}</span>' if pub else "") + "</li>")
        return "\n".join(out)

    sup = _src_items(payload.get("supporting"))
    con = _src_items(payload.get("contradicting"))
    sources_html = ""
    if sup:
        sources_html += f'<h3 class="s-sup">✓ Supporting evidence</h3><ul>{sup}</ul>'
    if con:
        sources_html += f'<h3 class="s-con">✗ Contradicting evidence</h3><ul>{con}</ul>'
    if not sources_html:
        sources_html = '<p class="muted">No public sources were attached to this verdict.</p>'

    created = _esc(rec.get("created_at", ""))

    # ── ClaimReview structured data (schema.org) ─────────────────
    # THE fact-checker distribution moat: valid ClaimReview markup lets Google
    # surface this page AS a fact-check (search rich result, Google News, Fact
    # Check Explorer) — a channel no chatbot answer can enter. Rating is mapped
    # to schema.org's required 1-5 scale from our 0-100 score + verdict band.
    if score >= 80:      _rating_val = 5
    elif score >= 62:    _rating_val = 4
    elif score >= 38:    _rating_val = 3
    elif score >= 20:    _rating_val = 2
    else:                _rating_val = 1
    _created_date = (rec.get("created_at", "") or "")[:10]
    _claimreview = {
        "@context": "https://schema.org",
        "@type": "ClaimReview",
        "url": page_url,
        "datePublished": _created_date,
        "claimReviewed": claim,
        "author": {"@type": "Organization", "name": "TruthScore", "url": base},
        "reviewRating": {
            "@type": "Rating",
            "ratingValue": _rating_val,
            "bestRating": 5,
            "worstRating": 1,
            "alternateName": verdict,
        },
        "itemReviewed": {"@type": "Claim", "name": claim},
    }
    # Escape "</" so the JSON can never break out of the <script> element.
    _jsonld = json.dumps(_claimreview, ensure_ascii=False).replace("</", "<\\/")

    # ── Tamper-evidence line ─────────────────────────────────────
    _integrity = _esc((rec.get("integrity", "") or "")[:16])
    _integrity_html = (
        f'<div class="foot">🔒 Integrity: <a href="{_esc(page_url)}/integrity">'
        f'{_integrity}…</a> — SHA-256 of this verdict; recompute to prove it was '
        f'not altered.</div>' if _integrity else "")

    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(desc)}">
<script type="application/ld+json">{_jsonld}</script>
<meta property="og:type" content="article">
<meta property="og:title" content="{_esc(title)}">
<meta property="og:description" content="{_esc(desc)}">
<meta property="og:url" content="{_esc(page_url)}">
<meta property="og:image" content="{_esc(card_url)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:site_name" content="TruthScore">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_esc(title)}">
<meta name="twitter:description" content="{_esc(desc)}">
<meta name="twitter:image" content="{_esc(card_url)}">
<style>
  :root{{color-scheme:dark}}
  body{{margin:0;background:#0f101c;color:#eeeef8;font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}}
  .wrap{{max-width:760px;margin:0 auto;padding:40px 22px 80px}}
  .brand{{color:#9898b8;font-weight:700;letter-spacing:.3px}}
  .verdict{{font-size:52px;font-weight:800;margin:18px 0 6px;color:{color}}}
  .score{{display:inline-block;background:#1e2034;border-radius:12px;padding:6px 14px;font-weight:700}}
  .claim{{font-size:22px;margin:22px 0;padding:16px 18px;background:#161826;border-left:4px solid {color};border-radius:8px}}
  .expl{{color:#c9c9e0;margin:18px 0}}
  h3{{margin:26px 0 8px;font-size:15px;text-transform:uppercase;letter-spacing:.5px}}
  .s-sup{{color:#2ecc71}} .s-con{{color:#e74c3c}}
  ul{{padding-left:20px;margin:6px 0}} li{{margin:6px 0}}
  a{{color:#7aa2ff}} .pub{{color:#9898b8;font-size:13px}}
  .muted{{color:#9898b8}}
  .cta{{display:inline-block;margin-top:34px;background:{color};color:#0f101c;font-weight:700;
        text-decoration:none;padding:12px 22px;border-radius:10px}}
  .foot{{margin-top:30px;color:#6d6d8a;font-size:13px}}
</style></head><body><div class="wrap">
  <div class="brand">TruthScore · verified fact check</div>
  <div class="verdict">{_esc(verdict)}</div>
  <div class="score">{score}/100 confidence</div>
  <div class="claim">{_esc(claim)}</div>
  <div class="expl">{_esc(explanation)}</div>
  {sources_html}
  <a class="cta" href="/">Check your own claim →</a>
  <div class="foot">Snapshot generated {created}. Verdicts reflect evidence available at check time.</div>
  {_integrity_html}
</div></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/v/{vid}/integrity")
async def verdict_integrity(vid: str):
    """Prove a verdict was not altered after publication. Recomputes the
    SHA-256 over the same fields the /v/{id} page displays and compares it to
    the hash stamped at save time. `intact=true` means the snapshot is
    byte-faithful. This verifiable-citation property is something a screenshot
    of a chatbot answer can never offer."""
    rec = await load_verdict(vid)
    if not rec:
        raise HTTPException(404, "Verdict not found")
    from pipeline.verdict_store import verdict_content_hash
    stored = rec.get("integrity", "") or ""
    recomputed = verdict_content_hash(rec)
    return {
        "id": vid,
        "algo": "sha256",
        "stored_hash": stored,
        "recomputed_hash": recomputed,
        # Legacy verdicts saved before integrity stamping have no stored hash;
        # report intact=null (unknown) rather than a misleading false.
        "intact": (stored == recomputed) if stored else None,
        "fields": ["id", "created_at", "claim", "verdict", "score",
                   "supporting_urls", "contradicting_urls"],
    }


@app.get("/v/{vid}/embed", include_in_schema=False)
async def verdict_embed(vid: str, request: Request):
    """A compact, self-contained badge for a verdict — designed to be dropped
    into any third-party page via an <iframe> (see /embed.js).

    This is a distribution moat ChatGPT structurally cannot match: a publisher,
    blogger, or journalist can stamp 'Verified by TruthScore: TRUE (87/100)' on
    their article, linking back to the frozen permalink. Every embed is both a
    trust signal for their readers and a backlink for us — the product spreads
    across the web as an accountability layer, not a chat window.
    """
    from fastapi.responses import HTMLResponse
    rec = await load_verdict(vid)
    if not rec:
        return HTMLResponse("<!doctype html><meta charset=utf-8><body style='margin:0'></body>",
                            status_code=404)
    claim = (rec.get("claim", "") or "")[:160]
    verdict = (rec.get("verdict", "UNCERTAIN") or "UNCERTAIN").upper()
    score = int(rec.get("score", 50))
    color = _VERDICT_PAGE_COLORS.get(verdict, "#95a5a6")
    base = str(request.base_url).rstrip("/")
    page_url = f"{base}/v/{vid}"
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  html,body{{margin:0}}
  a.badge{{display:flex;align-items:center;gap:12px;box-sizing:border-box;
    width:100%;max-width:520px;text-decoration:none;font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
    background:#12131f;color:#eeeef8;border:1px solid #2a2c40;border-left:5px solid {color};
    border-radius:12px;padding:12px 16px}}
  .v{{font-weight:800;color:{color};font-size:15px;letter-spacing:.3px;white-space:nowrap}}
  .sc{{background:#1e2034;border-radius:8px;padding:3px 9px;font-weight:700;font-size:13px;white-space:nowrap}}
  .cl{{color:#c9c9e0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .bp{{color:#8a8aa8;font-size:11px;white-space:nowrap}}
</style></head><body>
<a class="badge" href="{_esc(page_url)}" target="_blank" rel="noopener">
  <span class="v">{_esc(verdict)}</span>
  <span class="sc">{score}/100</span>
  <span class="cl" title="{_esc(claim)}">{_esc(claim)}</span>
  <span class="bp">✓ TruthScore</span>
</a></body></html>"""
    return HTMLResponse(html, headers={
        "Cache-Control": "public, max-age=3600",
        # Explicitly allow framing anywhere — the badge is meant to be embedded.
        "X-Frame-Options": "ALLOWALL",
        "Content-Security-Policy": "frame-ancestors *",
    })


@app.get("/embed.js", include_in_schema=False)
async def embed_js(request: Request):
    """Publisher embed snippet. A site drops:

        <div data-truthscore="VERDICT_ID"></div>
        <script async src="https://<host>/embed.js"></script>

    and every matching element is replaced with a responsive iframe rendering
    that verdict's badge. Self-contained, no dependencies, safe to cache hard."""
    from fastapi.responses import Response as _Resp
    base = str(request.base_url).rstrip("/")
    js = f"""(function(){{
  var ORIGIN={json.dumps(base)};
  function mount(el){{
    if(el.getAttribute('data-ts-mounted'))return;
    var id=el.getAttribute('data-truthscore');
    if(!id)return;
    el.setAttribute('data-ts-mounted','1');
    var f=document.createElement('iframe');
    f.src=ORIGIN+'/v/'+encodeURIComponent(id)+'/embed';
    f.title='TruthScore verdict';
    f.loading='lazy';
    f.setAttribute('scrolling','no');
    f.style.cssText='width:100%;max-width:520px;height:64px;border:0;overflow:hidden';
    el.innerHTML='';el.appendChild(f);
  }}
  function scan(){{
    var els=document.querySelectorAll('[data-truthscore]');
    for(var i=0;i<els.length;i++)mount(els[i]);
  }}
  if(document.readyState!=='loading')scan();
  else document.addEventListener('DOMContentLoaded',scan);
}})();"""
    return _Resp(content=js, media_type="application/javascript",
                 headers={"Cache-Control": "public, max-age=86400"})


@app.get("/accuracy", include_in_schema=False)
async def accuracy_page():
    """Public track-record page — radical transparency ChatGPT never offers.

    A fact-checker that publishes its OWN calibration (how often a claim scored
    ~80/100 actually turns out true) earns a kind of trust a black-box chatbot
    can't. We render the live ECE + the predicted→observed calibration curve +
    per-topic accuracy straight from real user feedback. When the sample is too
    small we say so honestly rather than fake a number."""
    from fastapi.responses import HTMLResponse
    try:
        rep = calibration_report()
    except Exception as e:
        print(f"[ACCURACY] report failed: {e}")
        rep = {"samples": 0, "enough_data": False, "ece": {}, "calibration_map": {}, "weak_domains": []}

    samples = int(rep.get("samples", 0) or 0)
    enough = bool(rep.get("enough_data"))
    ece_obj = rep.get("ece") or {}
    ece_val = ece_obj.get("ece")
    ece_verdict = ece_obj.get("verdict", "no-data")
    cal_map = rep.get("calibration_map") or {}
    weak = rep.get("weak_domains") or []

    # Calibration bars: predicted score bucket → observed accuracy.
    bars = ""
    for bucket in sorted(cal_map.keys(), key=lambda x: int(x)):
        acc = cal_map[bucket]
        col = "#2ecc71" if acc >= 62 else "#e74c3c" if acc < 38 else "#f1c40f"
        bars += (f'<div class="row"><span class="lbl">{_esc(str(bucket))}–{int(bucket)+9}</span>'
                 f'<span class="track"><span class="fill" style="width:{max(2,int(acc))}%;background:{col}"></span></span>'
                 f'<span class="val">{int(acc)}%</span></div>')
    if not bars:
        bars = '<p class="muted">Not enough feedback yet to plot the curve.</p>'

    weak_html = ""
    if weak:
        rows = "".join(
            f'<li>{_esc(w.get("topic",""))} — <b>{round(float(w.get("accuracy",0))*100)}%</b> '
            f'<span class="muted">({int(w.get("n",0))} samples)</span></li>'
            for w in weak[:8])
        weak_html = f'<h3>Where we\'re weakest (working on it)</h3><ul>{rows}</ul>'

    ece_display = ("—" if ece_val is None else f"{ece_val:.3f}")
    banner = ("" if enough else
              '<div class="note">⚠️ Early days — this track record is still being '
              'built from real user feedback. Numbers below stabilize as more '
              'verdicts get rated.</div>')

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TruthScore — Our Accuracy, In The Open</title>
<meta name="description" content="TruthScore publishes its own calibration and accuracy from real user feedback. Radical transparency.">
<style>
  :root{{color-scheme:dark}}
  body{{margin:0;background:#0f101c;color:#eeeef8;font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}}
  .wrap{{max-width:760px;margin:0 auto;padding:44px 22px 90px}}
  .brand{{color:#9898b8;font-weight:700;letter-spacing:.3px}}
  h1{{font-size:38px;margin:12px 0 4px;letter-spacing:-.5px}}
  .sub{{color:#c9c9e0;margin-bottom:26px}}
  .cards{{display:flex;gap:14px;flex-wrap:wrap;margin:20px 0}}
  .card{{flex:1;min-width:150px;background:#161826;border:1px solid #2a2c40;border-radius:14px;padding:18px}}
  .big{{font-size:34px;font-weight:800}}
  .clbl{{color:#9898b8;font-size:13px;margin-top:4px}}
  h3{{margin:30px 0 10px;font-size:15px;text-transform:uppercase;letter-spacing:.5px;color:#c9c9e0}}
  .row{{display:flex;align-items:center;gap:12px;margin:7px 0}}
  .lbl{{width:64px;color:#9898b8;font-size:13px;text-align:right}}
  .track{{flex:1;height:14px;background:#1e2034;border-radius:7px;overflow:hidden}}
  .fill{{display:block;height:100%}}
  .val{{width:44px;font-weight:700;font-size:13px}}
  .muted{{color:#9898b8}}
  .note{{background:#1e1a12;border:1px solid #4a3a18;border-radius:10px;padding:12px 16px;color:#e8c98a;margin:8px 0 20px}}
  ul{{padding-left:20px}} li{{margin:6px 0}}
  a.cta{{display:inline-block;margin-top:34px;background:#7c6cff;color:#0f101c;font-weight:700;text-decoration:none;padding:12px 22px;border-radius:10px}}
  .foot{{margin-top:26px;color:#6d6d8a;font-size:13px}}
</style></head><body><div class="wrap">
  <div class="brand">TruthScore</div>
  <h1>Our accuracy, in the open</h1>
  <div class="sub">Most AI tools ask you to trust them. We show you our receipts — calibration measured from real verdicts users rated.</div>
  {banner}
  <div class="cards">
    <div class="card"><div class="big">{samples}</div><div class="clbl">rated verdicts</div></div>
    <div class="card"><div class="big">{ece_display}</div><div class="clbl">calibration error (lower = better)</div></div>
    <div class="card"><div class="big" style="text-transform:capitalize">{_esc(ece_verdict)}</div><div class="clbl">calibration status</div></div>
  </div>
  <h3>When we say a score, how often are we right?</h3>
  <p class="muted" style="font-size:14px">Each bar: for claims we scored in that range, the share that turned out correct. A well-calibrated system tracks the diagonal.</p>
  {bars}
  {weak_html}
  <a class="cta" href="/">Check a claim →</a>
  <div class="foot">Computed live from user feedback. We publish the bad buckets too — that's the point.</div>
</div></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})


@app.get("/tokens.css", include_in_schema=False)
async def tokens_css():
    """Canonical design tokens shared with the extension (kept in sync).
    Referenced by Dashboard.html via <link rel="stylesheet" href="/tokens.css">."""
    from pathlib import Path
    path = Path(__file__).parent / "tokens.css"
    if path.exists():
        return FileResponse(path, media_type="text/css")
    return PlainTextResponse("", media_type="text/css")


@app.get("/site-config")
async def site_config():
    """Public, non-sensitive frontend configuration."""
    adsense_client = os.getenv("ADSENSE_CLIENT", "").strip()
    ads_flag = os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
    try:
        from utils.abuse import ANON_DAILY_CAP as _anon_cap
    except Exception:
        _anon_cap = 5
    return {
        "adsense_client": adsense_client,
        # Ads are only truly enabled when a publisher id is configured;
        # without one there is nothing to serve, so report False in dev.
        "ads_enabled": bool(ads_flag and adsense_client),
        "google_client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        # Single source of truth for the anonymous daily cap so the frontend
        # badge never drifts from the server's real limit.
        "anon_limit": _anon_cap,
        # House ad (internal "Upgrade to Pro") shows to free/anon users even
        # when AdSense has no inventory yet — always on unless explicitly off.
        "house_ads_enabled": os.getenv("HOUSE_ADS_ENABLED", "true").lower() in ("1", "true", "yes"),
    }


@app.get("/ads.txt", include_in_schema=False)
async def ads_txt():
    """AdSense authorization file — auto-generated from ADSENSE_CLIENT env."""
    pub = os.getenv("ADSENSE_CLIENT", "").strip().removeprefix("ca-pub-")
    if not pub:
        return PlainTextResponse("")
    return PlainTextResponse(
        f"google.com, pub-{pub}, DIRECT, f08c47fec0942fa0\n")




# ── Legal pages (Terms / Privacy / Refund) — bilingual EN default, ?lang=ro ──
# Rendered from legal_pages.py; company identity comes from config.COMPANY.
from legal_pages import render_legal


@app.get("/privacy", include_in_schema=False)
async def privacy_policy(lang: str = "en"):
    """Public Privacy Policy (required by Chrome Web Store & AdSense). ?lang=ro for Romanian."""
    return HTMLResponse(render_legal("privacy", lang))


@app.get("/refund", include_in_schema=False)
async def refund_policy(lang: str = "en"):
    """Refund & cancellation / EU right-of-withdrawal page. ?lang=ro for Romanian."""
    return HTMLResponse(render_legal("refund", lang))


@app.get("/terms", include_in_schema=False)
async def terms_of_service(lang: str = "en"):
    """Public Terms of Service (required by Stripe & Google OAuth). ?lang=ro for Romanian."""
    return HTMLResponse(render_legal("terms", lang))


# ── Auth endpoints ────────────────────────────────────────

@app.post("/auth/register")
async def register(data: UserRegister, request: Request):
    return await register_user(data, client_ip=_client_ip(request))


@app.post("/auth/login")
async def login(data: UserLogin, request: Request):
    return await login_user(data, client_ip=_client_ip(request))


@app.get("/related")
async def related_verdicts(q: str = "", limit: int = 3, exclude: str = ""):
    """Public 'has this been checked before?' lookup — the compounding
    knowledge base. Given a claim, return semantically-overlapping prior
    verdicts (each with its permanent /v/{id} link). This is a moat ChatGPT
    can't touch: it accumulates NO shared, citable fact base across users.
    Best-effort; returns an empty list on any issue."""
    try:
        from pipeline.verdict_store import find_similar_verdicts
        items = await find_similar_verdicts(q, limit=limit, exclude_id=exclude)
        return {"count": len(items), "items": items}
    except Exception as e:
        print(f"[RELATED] lookup skipped (non-fatal): {e}")
        return {"count": 0, "items": []}


@app.get("/auth/me")
async def me(user=Depends(require_user)):
    return await get_user_out(user)


@app.get("/me/history")
async def my_history(limit: int = 50, user=Depends(require_user)):
    """The signed-in user's private fact-check archive — every claim they've
    checked, newest first, each with its permanent /v/{id} permalink. This is a
    stickiness lever: your verdict history lives in TruthScore, not in an
    ephemeral chat. Best-effort; returns an empty list if the store is down."""
    try:
        from pipeline.verdict_store import list_user_verdicts
        uid = user.get("id") or ""
        items = await list_user_verdicts(uid, limit=limit)
        return {"count": len(items), "items": items}
    except Exception as e:
        print(f"[HISTORY] listing skipped (non-fatal): {e}")
        return {"count": 0, "items": []}


@app.post("/auth/logout")
async def logout(all_devices: bool = False,
                 credentials=Depends(_auth_security) if _auth_security else None):
    """Revoke the current session (default) or every session for the user
    (all_devices=true → bumps token_version, voiding all outstanding tokens)."""
    return await logout_user(credentials, all_devices=all_devices)


@app.post("/auth/google")
async def auth_google(req: GoogleAuthRequest):
    return await google_auth(req)


@app.get("/auth/google/callback")
async def auth_google_callback():
    return await google_callback()


class GoogleExchangeRequest(BaseModel):
    code: str
    code_verifier: str = ""
    redirect_uri: str = ""

@app.post("/auth/google/exchange")
async def auth_google_exchange(req: GoogleExchangeRequest):
    return await google_exchange(req.code, req.code_verifier, req.redirect_uri)


# ── Email verification & password reset endpoints ─────────────

@app.get("/auth/verify-email")
async def verify_email_endpoint(token: str):
    from auth import verify_email_token
    ok = await verify_email_token(token)
    if not ok:
        raise HTTPException(400, "Link invalid sau deja folosit")
    return HTMLResponse("""<!DOCTYPE html><html><head><meta charset=UTF-8>
    <title>Email verificat</title>
    <style>body{font-family:sans-serif;background:#0f0f17;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    .box{text-align:center;padding:40px;background:#1e1e2e;border-radius:16px;max-width:400px}
    h1{color:#22c55e;margin-bottom:12px}a{color:#a78bfa}</style></head>
    <body><div class="box"><h1>&#10003; Email verificat!</h1>
    <p>Contul tău TruthScore este acum verificat.</p>
    <p><a href="/">Mergi la TruthScore &rarr;</a></p></div></body></html>""")

class ForgotPasswordRequest(BaseModel):
    email: str

@app.post("/auth/forgot-password")
async def forgot_password_endpoint(req: ForgotPasswordRequest, request: Request):
    from auth import forgot_password
    await forgot_password(req.email, client_ip=_client_ip(request))
    return {"ok": True, "message": "Dacă emailul există, vei primi un link de resetare."}

class ResetPasswordRequest(BaseModel):
    token: str
    password: str

@app.post("/auth/reset-password")
async def reset_password_endpoint(req: ResetPasswordRequest, request: Request):
    from auth import reset_password
    await reset_password(req.token, req.password, client_ip=_client_ip(request))
    return {"ok": True, "message": "Parola a fost schimbată cu succes."}


# ── Payment endpoints ─────────────────────────────────────

@app.post("/stripe/checkout")
async def checkout(req: CheckoutRequest, user=Depends(require_user)):
    return await create_checkout(req, user)


@app.post("/stripe/webhook")
async def webhook(request: Request):
    return await stripe_webhook(request)


@app.post("/stripe/portal")
async def portal(user=Depends(require_user)):
    return await customer_portal(user)


@app.get("/plans")
async def plans():
    return PLANS


@app.get("/stats/public")
async def public_stats():
    """Aggregate stats for social proof: total claims checked, users, etc."""
    try:
        db = get_db()
        total_checks = await db.trending_claims.aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$check_count"}}}
        ]).to_list(1)
        total = (total_checks[0]["total"] if total_checks else 0)
        users = await db.users.estimated_document_count()
        claims = await db.trending_claims.estimated_document_count()
        return {"total_checks": total, "total_users": users, "unique_claims": claims}
    except Exception:
        return {"total_checks": 0, "total_users": 0, "unique_claims": 0}


@app.get("/refer/{code}", include_in_schema=False)
async def referral_redirect(code: str, request: Request):
    """Landing page for referral links — stores ref code in cookie then redirects home."""
    resp = RedirectResponse(url="/?ref=" + code.upper(), status_code=302)
    resp.set_cookie("ts_ref", code.upper(), max_age=86400 * 30, httponly=False, samesite="lax")
    return resp


@app.post("/auth/apply-referral")
async def apply_referral_endpoint(req: Request, user=Depends(require_user)):
    """Apply a referral code to the current user (one-time)."""
    try:
        body = await req.json()
        code = (body.get("ref_code") or "").strip()
        if not code:
            raise HTTPException(400, "ref_code required")
        from auth import apply_referral
        ok = await apply_referral(code, str(user["_id"]))
        if not ok:
            raise HTTPException(400, "Invalid or already-used referral code")
        return {"status": "ok", "bonus_checks": 5}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api-keys")
async def create_key(user=Depends(require_user)):
    """Create a new stable API key for the logged-in user."""
    from utils.api_keys import create_api_key, APIKeyLimitError
    plan = user.get("plan", "free")
    try:
        result = await create_api_key(str(user["_id"]), plan=plan)
    except APIKeyLimitError as e:
        raise HTTPException(429, str(e))
    return result


@app.get("/api-keys")
async def list_keys(user=Depends(require_user)):
    """List all active API keys for the logged-in user."""
    from utils.api_keys import list_api_keys
    keys = await list_api_keys(str(user["_id"]))
    return {"keys": keys, "count": len(keys)}


@app.delete("/api-keys/{key_id}")
async def revoke_key(key_id: str, user=Depends(require_user)):
    """Revoke an API key."""
    from utils.api_keys import revoke_api_key
    ok = await revoke_api_key(key_id, str(user["_id"]))
    if not ok:
        raise HTTPException(404, "API key negasita sau deja revocata")
    return {"status": "revoked", "key_id": key_id}


# ── Monitoring / Business intelligence ─────────────────────

@app.get("/metrics/calibration")
async def calibration_metrics(user=Depends(require_admin)):
    """Live model-calibration snapshot from real user feedback: Expected
    Calibration Error, the score→observed-accuracy map, and weakest domains.
    Admin-only. Drives the calibration loop (previously the ECE code read an
    offline CSV nothing produced; now it reads the durable feedback log)."""
    return calibration_report()


@app.get("/metrics/cost")
async def cost_metrics(user=Depends(require_admin)):
    """Real-time cost tracking (USD spent, per model). Admin-only — this is
    internal margin data and must never be public."""
    from utils.metrics import get_cost_summary
    from utils.metrics import estimate_cost_per_claim
    summary = get_cost_summary()
    return {
        "cost_usd": summary["cost_usd"],
        "calls_by_model": summary["models"],
        "total_calls": summary["calls"],
        "estimated_cost_per_claim": {
            plan: estimate_cost_per_claim(plan)
            for plan in ("free", "pro", "business", "enterprise")
        },
    }


@app.get("/metrics/quota")
async def quota_metrics(user=Depends(require_admin)):
    """Daily limits and current spend by plan (admin visibility)."""
    from utils.rate_limiter import PLAN_LIMITS
    from utils.evidence_cache import paid_search_budget_status
    return {
        "daily_limits": PLAN_LIMITS,
        "paid_search_budget": await paid_search_budget_status(),
    }


@app.get("/admin/data")
async def admin_dashboard_data(user=Depends(require_admin)):
    """Aggregate stats for the admin dashboard."""
    from datetime import datetime, timezone, timedelta
    try:
        db = get_db()
        # Users by plan
        plan_pipeline = [{"$group": {"_id": "$plan", "count": {"$sum": 1}}}]
        plan_docs = await db.users.aggregate(plan_pipeline).to_list(20)
        by_plan = {d["_id"] or "free": d["count"] for d in plan_docs}

        # New users per day (last 30 days)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)
        new_users_pipeline = [
            {"$match": {"created_at": {"$gte": cutoff}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        new_users_docs = await db.users.aggregate(new_users_pipeline).to_list(35)

        # Total users
        total_users = await db.users.estimated_document_count()

        # Checks per day from trending (last 30 days)
        checks_pipeline = [
            {"$match": {"last_checked": {"$gte": cutoff.isoformat()}}},
            {"$group": {"_id": {"$substr": ["$last_checked", 0, 10]}, "checks": {"$sum": "$check_count"}}},
            {"$sort": {"_id": 1}},
        ]
        checks_docs = await db.trending_claims.aggregate(checks_pipeline).to_list(35)

        # Total checks
        total_checks_agg = await db.trending_claims.aggregate(
            [{"$group": {"_id": None, "total": {"$sum": "$check_count"}}}]
        ).to_list(1)
        total_checks = (total_checks_agg[0]["total"] if total_checks_agg else 0)

        # Top claims
        top_claims = await db.trending_claims.find({}, {"claim": 1, "check_count": 1, "verdict": 1}).sort("check_count", -1).limit(10).to_list(10)
        for c in top_claims:
            c.pop("_id", None)

        # Revenue estimate
        prices = {"pro": 9.99, "annual_pro": 6.67, "business": 29.99, "annual_business": 19.99, "enterprise": 199}
        mrr = sum(by_plan.get(p, 0) * prices.get(p, 0) for p in prices)

        # Recent signups (last 10)
        recent_users = await db.users.find({}, {"email": 1, "plan": 1, "created_at": 1}).sort("created_at", -1).limit(10).to_list(10)
        for u in recent_users:
            u["_id"] = str(u["_id"])
            if hasattr(u.get("created_at"), "isoformat"):
                u["created_at"] = u["created_at"].isoformat()

        return {
            "total_users": total_users,
            "total_checks": total_checks,
            "mrr": round(mrr, 2),
            "by_plan": by_plan,
            "new_users_per_day": [{"date": d["_id"], "count": d["count"]} for d in new_users_docs],
            "checks_per_day": [{"date": d["_id"], "checks": d["checks"]} for d in checks_docs],
            "top_claims": top_claims,
            "recent_users": recent_users,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard():
    # Serve the admin shell freely — it contains no data. The page fetches
    # /admin/data (require_admin) with the JWT from localStorage, so real
    # protection stays on the data endpoint. Gating the shell on the header
    # broke direct URL navigation (browser sends no Authorization header).
    return HTMLResponse(_ADMIN_HTML)


class _SetPlanBody(BaseModel):
    email: str
    plan: str

@app.post("/admin/set-plan", include_in_schema=False)
async def admin_set_plan(body: _SetPlanBody, user=Depends(require_admin)):
    """Manually set a user's plan (admin only — for testing without Stripe)."""
    from auth import PLANS
    if body.plan not in PLANS:
        raise HTTPException(400, f"Unknown plan '{body.plan}'. Valid: {list(PLANS)}")
    db = get_db()
    result = await db.users.update_one(
        {"email": body.email.lower().strip()},
        {"$set": {"plan": body.plan, "stripe_subscription_id": f"manual_{body.plan}"}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, f"User '{body.email}' not found")
    return {"ok": True, "email": body.email, "plan": body.plan}



_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TruthScore Admin</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d1a;color:#e5e7eb;font-family:'Inter',-apple-system,sans-serif;padding:24px}
h1{font-size:24px;font-weight:800;margin-bottom:24px;color:#fff}
h2{font-size:16px;font-weight:700;margin-bottom:14px;color:#c4b5fd}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:28px}
.card{background:#1a1a2e;border:1px solid rgba(109,40,217,.2);border-radius:12px;padding:20px}
.stat-val{font-size:32px;font-weight:900;color:#fff;letter-spacing:-1px}
.stat-lbl{font-size:12px;color:#9ca3af;margin-top:4px}
.chart-wrap{background:#1a1a2e;border:1px solid rgba(109,40,217,.2);border-radius:12px;padding:20px;margin-bottom:24px}
.charts-2col{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;color:#9ca3af;font-weight:600;border-bottom:1px solid rgba(255,255,255,.07)}
td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.04)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-free{background:rgba(107,114,128,.2);color:#9ca3af}
.badge-pro{background:rgba(109,40,217,.2);color:#c4b5fd}
.badge-business{background:rgba(251,191,36,.15);color:#fbbf24}
.badge-enterprise{background:rgba(34,197,94,.15);color:#22c55e}
@media(max-width:700px){.charts-2col{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>🛡️ TruthScore Admin Dashboard</h1>
<div class="grid" id="kpiGrid"><div style="color:#6b7280;font-size:14px">Loading…</div></div>
<div class="charts-2col">
  <div class="chart-wrap"><h2>New Users (30 days)</h2><canvas id="usersChart" height="200"></canvas></div>
  <div class="chart-wrap"><h2>Checks / Day (30 days)</h2><canvas id="checksChart" height="200"></canvas></div>
</div>
<div class="charts-2col">
  <div class="chart-wrap"><h2>Users by Plan</h2><canvas id="planChart" height="220"></canvas></div>
  <div class="chart-wrap">
    <h2>Top 10 Claims</h2>
    <table><thead><tr><th>Claim</th><th>Verdict</th><th>Checks</th></tr></thead><tbody id="topClaims"></tbody></table>
  </div>
</div>
<div class="chart-wrap">
  <h2>Recent Signups</h2>
  <table><thead><tr><th>Email</th><th>Plan</th><th>Signed up</th></tr></thead><tbody id="recentUsers"></tbody></table>
</div>
<script>
const token = localStorage.getItem('ts_token') || new URLSearchParams(location.search).get('token') || '';
const hdr = token ? {'Authorization':'Bearer '+token} : {};
const PLAN_COLORS = {free:'#6b7280',pro:'#7c3aed',annual_pro:'#9333ea',business:'#f59e0b',annual_business:'#f97316',enterprise:'#22c55e'};
function fmt(n){return n>=1000000?(n/1000000).toFixed(1)+'M':n>=1000?(n/1000).toFixed(1)+'k':String(n);}
function badgeClass(p){return 'badge badge-'+(p||'free').split('_').pop();}
function lineChart(id,labels,data,label,color){
  new Chart(document.getElementById(id),{type:'line',data:{labels,datasets:[{label,data,borderColor:color,backgroundColor:color+'22',fill:true,tension:.4,pointRadius:3}]},options:{plugins:{legend:{display:false}},scales:{x:{grid:{color:'rgba(255,255,255,.05)'},ticks:{color:'#9ca3af',font:{size:10}}},y:{grid:{color:'rgba(255,255,255,.05)'},ticks:{color:'#9ca3af',font:{size:10}}}}}});
}
async function load(){
  try{
    const r = await fetch('/admin/data',{headers:hdr});
    if(r.status===401||r.status===403){document.body.innerHTML='<div style="padding:40px;text-align:center;color:#ef4444">Access denied. Are you logged in as admin?</div>';return;}
    const d = await r.json();
    // KPI cards
    document.getElementById('kpiGrid').innerHTML=[
      ['Total Users', fmt(d.total_users),'👥'],
      ['Total Checks', fmt(d.total_checks),'✅'],
      ['Est. MRR', '€'+d.mrr.toFixed(2),'💰'],
      ['Paid Users', fmt(Object.entries(d.by_plan||{}).filter(([k])=>k!=='free').reduce((s,[,v])=>s+v,0)),'⭐'],
    ].map(([l,v,e])=>`<div class="card"><div class="stat-val">${e} ${v}</div><div class="stat-lbl">${l}</div></div>`).join('');
    // Line charts
    const uLabels = (d.new_users_per_day||[]).map(x=>x.date.slice(5));
    const uData = (d.new_users_per_day||[]).map(x=>x.count);
    lineChart('usersChart',uLabels,uData,'New Users','#7c3aed');
    const cLabels = (d.checks_per_day||[]).map(x=>x.date.slice(5));
    const cData = (d.checks_per_day||[]).map(x=>x.checks);
    lineChart('checksChart',cLabels,cData,'Checks','#06b6d4');
    // Plan pie
    const planEntries = Object.entries(d.by_plan||{});
    new Chart(document.getElementById('planChart'),{type:'doughnut',data:{labels:planEntries.map(([k])=>k),datasets:[{data:planEntries.map(([,v])=>v),backgroundColor:planEntries.map(([k])=>PLAN_COLORS[k]||'#6b7280')}]},options:{plugins:{legend:{position:'right',labels:{color:'#9ca3af',font:{size:12}}}}}});
    // Top claims table
    document.getElementById('topClaims').innerHTML=(d.top_claims||[]).map(c=>`<tr><td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${c.claim||''}">${(c.claim||'').substring(0,70)}</td><td>${c.verdict||'—'}</td><td>${c.check_count||0}</td></tr>`).join('');
    // Recent users
    document.getElementById('recentUsers').innerHTML=(d.recent_users||[]).map(u=>`<tr><td>${u.email||''}</td><td><span class="${badgeClass(u.plan)}">${u.plan||'free'}</span></td><td style="color:#6b7280;font-size:11px">${(u.created_at||'').slice(0,10)}</td></tr>`).join('');
  }catch(e){document.getElementById('kpiGrid').innerHTML='<div style="color:#ef4444">Error: '+e.message+'</div>';}
}
load();
</script>
</body>
</html>"""

@app.post("/batch-verify")
async def batch(req: BatchVerifyRequest, user=Depends(require_user)):
    return await batch_verify(req, user)


class SteelManRequest(BaseModel):
    claim: str = ""
    verdict: str = ""
    score: int = 50


@app.post("/steel-man")
async def steel_man(req: SteelManRequest, request: Request,
                    current_user=Depends(get_current_user)):
    """Generate the strongest possible counter-argument for a verified claim."""
    claim = (req.claim or "").strip()[:2000]
    if not claim:
        return {"steel_man": "", "key_points": []}
    # Meter against the daily quota — this makes a real LLM call, so leaving it
    # unmetered let anyone rack up cost / bypass the verify limit.
    await enforce_quota(current_user, claim, client_ip=_client_ip(request),
                        fp=_client_fp(request))
    verdict_ctx = f"The claim was rated {req.verdict} with a score of {req.score}/100." if req.verdict else ""
    prompt = (
        f"A fact-checking system has evaluated the following claim:\n\n"
        f"CLAIM: {claim}\n"
        f"{verdict_ctx}\n\n"
        f"Your task: produce the STRONGEST possible counter-argument — the most credible, well-sourced "
        f"case that challenges this verdict. Steel-man the opposing view even if you agree with the verdict. "
        f"Be specific and factual. Avoid rhetoric.\n\n"
        f"Reply in JSON only:\n"
        f"{{\"steel_man\": \"<2-3 sentence counter-argument>\", "
        f"\"key_points\": [\"<point 1>\", \"<point 2>\", \"<point 3>\"]}}"
    )
    try:
        from pipeline.reasoning import call_llm_raw
        raw = await call_llm_raw(prompt, max_tokens=400, model="groq")
        import re as _re
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if m:
            data = json.loads(m.group())
            return {
                "steel_man": str(data.get("steel_man", "") or ""),
                "key_points": [str(p) for p in (data.get("key_points") or [])[:5]],
            }
    except Exception as e:
        print(f"[steel-man] error: {e}")
    return {"steel_man": "Unable to generate counter-argument at this time.", "key_points": []}


# ── Watch This Claim ────────────────────────────────────────────

class WatchRequest(BaseModel):
    claim: str = ""
    verdict: str = ""
    score: int = 50

@app.post("/watch")
async def watch_claim(req: WatchRequest, user=Depends(require_user)):
    """Save a claim to the user's watch list."""
    claim = (req.claim or "").strip()[:2000]
    if not claim:
        raise HTTPException(400, "claim required")
    try:
        from auth import get_db
        db = get_db()
        col = db["watched_claims"]
        existing = await col.find_one({"user_id": user["id"], "claim": claim})
        if existing:
            return {"id": str(existing["_id"]), "already_watching": True}
        doc = {
            "user_id": user["id"],
            "claim": claim,
            "last_verdict": req.verdict or "UNCERTAIN",
            "last_score": req.score,
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        result = await col.insert_one(doc)
        return {"id": str(result.inserted_id), "already_watching": False}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/watch")
async def list_watched(user=Depends(require_user)):
    """List all watched claims for the user."""
    try:
        from auth import get_db
        db = get_db()
        col = db["watched_claims"]
        docs = await col.find({"user_id": user["id"]}).sort("created_at", -1).to_list(100)
        for d in docs:
            d["id"] = str(d.pop("_id", ""))
        return {"items": docs}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/watch/{watch_id}")
async def unwatch_claim(watch_id: str, user=Depends(require_user)):
    """Remove a claim from watch list."""
    try:
        from auth import get_db
        from bson import ObjectId
        db = get_db()
        col = db["watched_claims"]
        result = await col.delete_one({"_id": ObjectId(watch_id), "user_id": user["id"]})
        return {"deleted": result.deleted_count > 0}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Trending claims (public) ──────────────────────────────
@app.get("/trending")
async def trending(limit: int = 10):
    """Most-checked claims right now. Public — no auth."""
    try:
        from auth import get_db
        from pipeline.trending import get_trending
        db = get_db()
        items = await get_trending(db, limit=limit)
        return {"items": items}
    except Exception as e:
        print(f"[TRENDING] endpoint failed (non-fatal): {e}")
        return {"items": []}


@app.get("/public-stats")
async def public_stats_detailed():
    """Aggregate public stats for the accuracy/trust page."""
    try:
        from auth import get_db
        from pipeline.trending import get_public_stats
        db = get_db()
        return await get_public_stats(db)
    except Exception:
        return {"total_checks": 0, "total_false": 0, "total_true": 0,
                "total_uncertain": 0, "top_topics": []}


# ── URL fact-check ────────────────────────────────────────
class UrlCheckRequest(BaseModel):
    url: str

@app.post("/check-url")
async def check_url_endpoint(req: UrlCheckRequest, request: Request,
                            current_user=Depends(get_current_user)):
    """Fetch a URL, extract text, and fact-check its claims."""
    from auth import get_db
    from api.url_checker import check_url
    # Fetches + fact-checks arbitrary URL content (heavy LLM path) — meter it so
    # it counts against the daily quota like a normal check.
    await enforce_quota(current_user, req.url, client_ip=_client_ip(request),
                        fp=_client_fp(request))
    db = get_db()
    return await check_url(req.url, db, user=current_user)


# ── Entity profile (public) ───────────────────────────────
@app.get("/entity/{name}")
async def entity_profile(name: str):
    """Public reliability profile for a named entity (person/company/etc.)."""
    try:
        from auth import get_db
        from pipeline.entity_memory import get_entity_profile
        db = get_db()
        prof = await get_entity_profile(db, name)
        if not prof:
            raise HTTPException(404, "No profile for this entity")
        prof.pop("_id", None)
        return prof
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "No profile for this entity")


# ── Source credibility (public) ───────────────────────────
@app.get("/source-credibility")
async def source_credibility_top(limit: int = 20):
    """Top reliable source domains, ranked by accumulated reliability score."""
    try:
        from auth import get_db
        from pipeline.source_credibility import get_top_sources
        db = get_db()
        return {"sources": await get_top_sources(db, limit=limit)}
    except Exception:
        return {"sources": []}


# ── Per-domain source page (public, SEO-indexed) ──────────
@app.get("/source/{domain}", response_class=HTMLResponse)
async def source_domain_page(domain: str):
    """Permanent, Google-indexed reliability profile for one source domain."""
    from auth import get_db
    from pipeline.source_credibility import extract_domain, get_domain_score, render_source_page
    norm = extract_domain(domain)
    if not norm:
        raise HTTPException(404, "Unknown source")
    doc = await get_domain_score(get_db(), norm)
    if not doc:
        raise HTTPException(404, "No credibility data for this source yet")
    return HTMLResponse(render_source_page(doc, PUBLIC_BASE_URL))


# ── Daily challenge (public) ──────────────────────────────
@app.get("/challenge")
async def daily_challenge():
    """Today's fact-checking challenge — guess the verdict."""
    from auth import get_db
    from api.challenge import get_daily_challenge
    db = get_db()
    return await get_daily_challenge(db)


class ChallengeAnswerRequest(BaseModel):
    id: str
    guess: str

@app.post("/challenge/answer")
async def answer_daily_challenge(req: ChallengeAnswerRequest, user=Depends(get_current_user)):
    from auth import get_db
    from api.challenge import answer_challenge, submit_challenge_score
    db = get_db()
    result = await answer_challenge(db, req.id, req.guess)
    # Record score for logged-in users
    if user:
        try:
            await submit_challenge_score(db, user["id"], req.id, result.get("correct", False))
        except Exception:
            pass
        try:
            from auth import touch_streak
            result["streak"] = await touch_streak(user["id"])
        except Exception:
            pass
    return result


@app.get("/challenge/leaderboard")
async def challenge_leaderboard(limit: int = 10):
    """Top 10 users by correct challenge answers."""
    try:
        from auth import get_db
        from api.challenge import get_leaderboard
        return {"leaderboard": await get_leaderboard(get_db(), limit=min(limit, 50))}
    except Exception:
        return {"leaderboard": []}


# ── Web Push Notifications ────────────────────────────────
@app.get("/push/vapid-key")
async def push_vapid_key():
    """Return the VAPID public key for Web Push subscription."""
    from api.push_notifications import get_vapid_public_key
    return {"public_key": get_vapid_public_key()}

class PushSubscribeRequest(BaseModel):
    subscription: dict

@app.post("/push/subscribe")
async def push_subscribe(req: PushSubscribeRequest, user=Depends(require_user)):
    from auth import get_db
    from api.push_notifications import subscribe
    ok = await subscribe(get_db(), user["id"], req.subscription)
    return {"ok": ok}

@app.delete("/push/subscribe")
async def push_unsubscribe(endpoint: str, user=Depends(require_user)):
    from auth import get_db
    from api.push_notifications import unsubscribe
    ok = await unsubscribe(get_db(), endpoint, user["id"])
    return {"ok": ok}


# ── Public Claim Pages (SEO-indexed) ─────────────────────
@app.get("/claim/{slug}", response_class=HTMLResponse)
async def public_claim_page(slug: str):
    """Serve a permanent, Google-indexed fact-check page for a claim."""
    from auth import get_db
    from pipeline.public_claims import get_public_claim, render_claim_page
    doc = await get_public_claim(get_db(), slug)
    if not doc:
        raise HTTPException(404, "Claim not found")
    return HTMLResponse(render_claim_page(doc, PUBLIC_BASE_URL))


# ── Public Article Pages (SEO-indexed URL fact-checks) ────
@app.get("/article/{slug}", response_class=HTMLResponse)
async def public_article_page(slug: str):
    """Serve a permanent, Google-indexed fact-check page for a checked URL."""
    from auth import get_db
    from pipeline.articles import get_article, render_article_page
    doc = await get_article(get_db(), slug)
    if not doc:
        raise HTTPException(404, "Article not found")
    return HTMLResponse(render_article_page(doc, PUBLIC_BASE_URL))


@app.get("/claim/{slug}/og.svg", response_class=PlainTextResponse)
async def claim_og_svg(slug: str):
    """SVG share card for a claim — used as OG image for social sharing."""
    from auth import get_db
    from pipeline.public_claims import get_public_claim
    doc = await get_public_claim(get_db(), slug)
    if not doc:
        raise HTTPException(404, "Claim not found")

    claim = (doc.get("claim") or "")[:120]
    verdict = doc.get("verdict", "UNCERTAIN")
    score = int(doc.get("score", 50))

    vcolors = {"TRUE": "#22c55e", "FALSE": "#ef4444"}
    vcolor = vcolors.get(verdict, "#f59e0b")
    vemoji = {"TRUE": "✅", "FALSE": "❌"}.get(verdict, "⚠️")
    scolor = "#22c55e" if score >= 70 else ("#f59e0b" if score >= 40 else "#ef4444")

    # Wrap claim text at ~45 chars per line, max 3 lines
    import textwrap
    lines = textwrap.wrap(claim, 45)[:3]
    claim_svg_lines = "".join(
        f'<text x="40" y="{180 + i*38}" font-size="22" fill="#e2e8f0" font-family="system-ui,sans-serif">{l}</text>'
        for i, l in enumerate(lines)
    )
    bar_w = int(score * 5.2)  # 520px max width for score bar

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="320" viewBox="0 0 600 320">
  <rect width="600" height="320" fill="#0f0f17" rx="16"/>
  <rect x="0" y="0" width="4" height="320" fill="{vcolor}" rx="2"/>
  <text x="40" y="60" font-size="14" fill="#6b7280" font-family="system-ui,sans-serif" font-weight="600" letter-spacing="2">TRUTHSCORE FACT CHECK</text>
  <text x="40" y="110" font-size="14" fill="#9ca3af" font-family="system-ui,sans-serif">Claim:</text>
  {claim_svg_lines}
  <rect x="40" y="{220 + len(lines)*38}" width="{bar_w}" height="8" fill="{scolor}" rx="4"/>
  <rect x="40" y="{220 + len(lines)*38}" width="520" height="8" fill="#2a2a3e" rx="4"/>
  <text x="40" y="{220 + len(lines)*38 + 8}" dominant-baseline="auto" font-size="14" fill="{scolor}" font-family="system-ui,sans-serif" font-weight="700" dy="24">{score}/100</text>
  <rect x="430" y="60" width="130" height="44" fill="{vcolor}22" rx="22"/>
  <text x="495" y="89" font-size="20" fill="{vcolor}" font-family="system-ui,sans-serif" font-weight="800" text-anchor="middle">{vemoji} {verdict}</text>
  <text x="40" y="305" font-size="12" fill="#4b5563" font-family="system-ui,sans-serif">truthscore.app</text>
</svg>'''
    return PlainTextResponse(svg, media_type="image/svg+xml")


@app.get("/sitemap.xml", response_class=PlainTextResponse)
async def sitemap_xml():
    """XML sitemap listing all public claim/article/source pages for Google."""
    from auth import get_db
    from pipeline.public_claims import list_slugs_for_sitemap
    from pipeline.articles import list_article_slugs_for_sitemap
    from pipeline.source_credibility import list_domains_for_sitemap
    db = get_db()
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
             f'  <url><loc>{PUBLIC_BASE_URL}/</loc><priority>1.0</priority></url>']

    for row in await list_slugs_for_sitemap(db, limit=5000):
        loc = f"{PUBLIC_BASE_URL}/claim/{row['_id']}"
        lastmod = (row.get("updated_at") or "")[:10]
        mod_tag = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        lines.append(f"  <url><loc>{loc}</loc>{mod_tag}<priority>0.8</priority></url>")

    for row in await list_article_slugs_for_sitemap(db, limit=5000):
        loc = f"{PUBLIC_BASE_URL}/article/{row['_id']}"
        lastmod = (row.get("updated_at") or "")[:10]
        mod_tag = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        lines.append(f"  <url><loc>{loc}</loc>{mod_tag}<priority>0.7</priority></url>")

    for row in await list_domains_for_sitemap(db, limit=5000):
        loc = f"{PUBLIC_BASE_URL}/source/{row['_id']}"
        lastmod = (row.get("last_updated") or "")[:10]
        mod_tag = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        lines.append(f"  <url><loc>{loc}</loc>{mod_tag}<priority>0.6</priority></url>")

    lines.append("</urlset>")
    return PlainTextResponse("\n".join(lines), media_type="application/xml")


# ── Webhooks (authenticated) ──────────────────────────────
class WebhookCreateRequest(BaseModel):
    url: str
    events: list[str] = ["verdict_change"]

@app.post("/webhooks")
async def create_webhook_endpoint(req: WebhookCreateRequest, user=Depends(require_user)):
    from auth import get_db
    from api.webhooks import create_webhook
    db = get_db()
    return await create_webhook(db, user["id"], req.url, req.events)

@app.get("/webhooks")
async def list_webhooks_endpoint(user=Depends(require_user)):
    from auth import get_db
    from api.webhooks import list_webhooks
    db = get_db()
    return {"webhooks": await list_webhooks(db, user["id"])}

@app.delete("/webhooks/{webhook_id}")
async def delete_webhook_endpoint(webhook_id: str, user=Depends(require_user)):
    from auth import get_db
    from api.webhooks import delete_webhook
    db = get_db()
    return {"deleted": await delete_webhook(db, user["id"], webhook_id)}


# ── Claim Timeline / Version History ──────────────────────────────

@app.get("/v/{verdict_id}/history")
async def verdict_history(verdict_id: str):
    """Return the truth-over-time history for a stored verdict.

    Resolves the verdict's claim text, then reads the shared temporal-drift
    timeline (keyed by claim, populated on every /verify) so this returns real
    history instead of an always-empty list.
    """
    try:
        from pipeline.verdict_store import load_verdict
        from pipeline.temporal_drift import get_truth_timeline
        from auth import get_db
        db = get_db()
        current = await load_verdict(verdict_id)
        if not current:
            raise HTTPException(404, "Verdict not found")
        claim = current.get("claim", "") if isinstance(current, dict) else ""
        timeline = await get_truth_timeline(db, claim) if claim else []
        return {"verdict_id": verdict_id, "claim": claim,
                "history": timeline, "current": current}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))



# ── WhatsApp Bot Webhook ───────────────────────────────────────────

@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    """
    Receives WhatsApp Business Cloud API (Meta) webhook events.
    Set up: Meta Developer Console → WhatsApp → Webhook → https://<host>/whatsapp/webhook
    Required env: WHATSAPP_TOKEN, WHATSAPP_VERIFY_TOKEN
    """
    # Verification challenge (GET is handled separately)
    wa_token = os.getenv("WHATSAPP_TOKEN", "")
    if not wa_token:
        return {"status": "WHATSAPP_TOKEN not configured"}
    try:
        body = await request.json()
        from whatsapp_bot import handle_whatsapp_update
        import asyncio as _asyncio
        _asyncio.create_task(handle_whatsapp_update(body, wa_token))
    except Exception as e:
        print(f"[whatsapp-webhook] error: {e}")
    return {"status": "ok"}


@app.get("/whatsapp/webhook")
async def whatsapp_verify(
    hub_mode: str = "",
    hub_challenge: str = "",
    hub_verify_token: str = "",
):
    """Meta webhook verification challenge."""
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "truthscore_verify")
    if hub_mode == "subscribe" and hub_verify_token == expected:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(403, "Invalid verify token")


# ── Publisher Trust Ranking ────────────────────────────────────────

@app.get("/publishers/ranking")
async def publisher_ranking(limit: int = 30):
    """
    Aggregate source stats from stored verdicts to build a publisher trust ranking.
    Returns publishers sorted by reliability score (weighted by appearance count).
    """
    try:
        from auth import get_db
        db = get_db()
        col = db["verdicts"]
        # Aggregate: for each source domain, count appearances and average score
        pipeline_agg = [
            {"$project": {"sources": {"$concatArrays": [
                {"$ifNull": ["$supporting", []]},
                {"$ifNull": ["$contradicting", []]},
                {"$ifNull": ["$neutral_sources", []]}
            ]}}},
            {"$unwind": "$sources"},
            {"$group": {
                "_id": "$sources.publisher",
                "count": {"$sum": 1},
                "supporting": {"$sum": {"$cond": [{"$in": ["$sources", "$supporting"]}, 1, 0]}},
                "urls": {"$addToSet": "$sources.url"},
            }},
            {"$match": {"_id": {"$ne": None}, "_id": {"$ne": ""}, "count": {"$gte": 2}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        docs = await col.aggregate(pipeline_agg).to_list(limit)
        # Merge with DOMAIN_CRED for reliability score
        results = []
        for d in docs:
            publisher = d.get("_id", "")
            domain = ""
            for url in (d.get("urls") or [])[:3]:
                try:
                    from urllib.parse import urlparse
                    domain = urlparse(url).hostname or ""
                    domain = domain.replace("www.", "")
                    if domain:
                        break
                except Exception:
                    pass
            results.append({
                "publisher": publisher,
                "domain": domain,
                "appearances": d["count"],
            })
        return {"publishers": results, "total": len(results)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/verify-pdf")
async def pdf(req: VerifyRequest, user=Depends(require_user)):
    return await verify_and_pdf(req, user)


@app.post("/generate-pdf", include_in_schema=False)
async def generate_pdf_from_result(request: Request, user=Depends(require_user)):
    """Generate PDF from an already-computed result dict (instant — no re-verification)."""
    from fastapi.responses import Response as FResponse
    if not PDF_AVAILABLE:
        raise HTTPException(503, "PDF not configured")
    plan_name = user.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS["free"])
    if not plan["pdf"]:
        raise HTTPException(403, "PDF reports require Pro plan or higher")
    body = await request.json()
    from pdf_report import generate_pdf_report
    pdf_bytes = generate_pdf_report(body)
    verdict = (body.get("verdict") or "report").lower()
    score   = body.get("score", 0)
    return FResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="truthscore_{verdict}_{score}.pdf"'},
    )


# ── Telegram Bot Webhook ───────────────────────────────────────

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receives Telegram Bot API updates and processes them."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # Optional secret token validation (set via setWebhook secretToken param)
    tg_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if tg_secret and secret != tg_secret:
        raise HTTPException(403, "Invalid webhook secret")
    try:
        update = await request.json()
        from telegram_bot import handle_update
        import asyncio as _asyncio
        _asyncio.create_task(handle_update(update))
    except Exception as e:
        print(f"[telegram-webhook] error: {e}")
    return {"ok": True}


# ── Daily Email Digest ─────────────────────────────────────────────

class DigestSubscribeRequest(BaseModel):
    email: str = ""

@app.post("/digest/subscribe")
async def digest_subscribe(req: DigestSubscribeRequest):
    """Subscribe an email to the daily fact-check digest."""
    email = (req.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    try:
        from auth import get_db
        db = get_db()
        col = db["digest_subscribers"]
        existing = await col.find_one({"email": email})
        if existing:
            if not existing.get("active"):
                await col.update_one({"email": email}, {"$set": {"active": True}})
                return {"status": "resubscribed"}
            return {"status": "already_subscribed"}
        await col.insert_one({
            "email": email,
            "active": True,
            "subscribed_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": "subscribed"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/digest/unsubscribe")
async def digest_unsubscribe(req: DigestSubscribeRequest):
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email required")
    try:
        from auth import get_db
        db = get_db()
        await db["digest_subscribers"].update_one({"email": email}, {"$set": {"active": False}})
        return {"status": "unsubscribed"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/digest/send")
async def digest_send(user=Depends(require_admin)):
    """Admin: trigger daily digest send."""
    from email_digest import run_digest
    from auth import get_db
    db = get_db()
    result = await run_digest(db)
    return result


@app.post("/digest/send-weekly-lies")
async def digest_send_weekly_lies(user=Depends(require_admin)):
    """Admin: trigger the 'Top 5 Lies of the Week' digest send."""
    from email_digest import run_weekly_lies_digest
    from auth import get_db
    db = get_db()
    return await run_weekly_lies_digest(db)


# ── News Scanner ───────────────────────────────────────────────────

@app.post("/news-scanner/run")
async def news_scanner_run(user=Depends(require_admin)):
    """Admin: run the news scanner to auto-verify today's headlines."""
    from news_scanner import run_scan
    from auth import get_db
    db = get_db()
    import asyncio as _asyncio
    _asyncio.create_task(run_scan(db))
    return {"status": "started"}


# ── Claim Monitors (B2B) ──────────────────────────────────────────────────────

class MonitorCreate(BaseModel):
    name: str
    keyword: str
    notify_email: str = ""

@app.post("/monitors")
async def create_monitor(req: MonitorCreate, user=Depends(require_user)):
    """Create a keyword monitor. Alerts when new claims matching keyword appear."""
    from bson import ObjectId
    plan = user.get("plan", "free")
    feat = PLANS.get(plan, PLANS["free"]).get("features", {})
    max_monitors = feat.get("monitors", 0)
    if max_monitors == 0:
        raise HTTPException(403, "Upgrade to Monitor/Business plan to use claim monitors.")
    db = get_db()
    count = await db.monitors.count_documents({"user_id": user["_id"], "active": True})
    if max_monitors != -1 and count >= max_monitors:
        raise HTTPException(400, f"You've reached your monitor limit ({max_monitors}). Upgrade to add more.")
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    doc = {
        "user_id": user["_id"],
        "name": req.name[:100],
        "keyword": req.keyword[:100].lower(),
        "notify_email": req.notify_email or user.get("email", ""),
        "active": True,
        "created_at": now,
        "last_alert": None,
    }
    result = await db.monitors.insert_one(doc)
    return {"id": str(result.inserted_id), "keyword": doc["keyword"], "name": doc["name"]}

@app.get("/monitors")
async def list_monitors(user=Depends(require_user)):
    db = get_db()
    docs = await db.monitors.find({"user_id": user["_id"]}).sort("created_at", -1).to_list(50)
    for d in docs:
        d["id"] = str(d.pop("_id"))
        d["user_id"] = str(d["user_id"])
    return docs

@app.delete("/monitors/{monitor_id}")
async def delete_monitor(monitor_id: str, user=Depends(require_user)):
    from bson import ObjectId
    db = get_db()
    try:
        result = await db.monitors.delete_one({"_id": ObjectId(monitor_id), "user_id": user["_id"]})
        if result.deleted_count == 0:
            raise HTTPException(404, "Monitor not found")
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(400, str(e))


# ── Community Voting on Claims ────────────────────────────────────────────────

@app.post("/claim/{slug}/vote")
async def vote_on_claim(slug: str, request: Request, user=Depends(get_current_user)):
    """Cast a community vote on a public claim verdict (agree/disagree)."""
    try:
        body = await request.json()
        vote = body.get("vote")  # "agree" or "disagree"
        if vote not in ("agree", "disagree"):
            raise HTTPException(400, "vote must be 'agree' or 'disagree'")
        db = get_db()
        voter_id = str(user["_id"]) if user else _client_ip(request)
        now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        await db.claim_votes.update_one(
            {"slug": slug, "voter": voter_id},
            {"$set": {"vote": vote, "updated_at": now, "slug": slug, "voter": voter_id}},
            upsert=True,
        )
        # Return updated tally
        pipeline = [
            {"$match": {"slug": slug}},
            {"$group": {"_id": "$vote", "count": {"$sum": 1}}},
        ]
        tally_docs = await db.claim_votes.aggregate(pipeline).to_list(5)
        tally = {d["_id"]: d["count"] for d in tally_docs}
        total = sum(tally.values())
        return {
            "agree": tally.get("agree", 0),
            "disagree": tally.get("disagree", 0),
            "total": total,
            "agree_pct": round(tally.get("agree", 0) / max(total, 1) * 100),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/claim/{slug}/votes")
async def get_claim_votes(slug: str):
    try:
        db = get_db()
        pipeline = [
            {"$match": {"slug": slug}},
            {"$group": {"_id": "$vote", "count": {"$sum": 1}}},
        ]
        tally_docs = await db.claim_votes.aggregate(pipeline).to_list(5)
        tally = {d["_id"]: d["count"] for d in tally_docs}
        total = sum(tally.values())
        return {
            "agree": tally.get("agree", 0),
            "disagree": tally.get("disagree", 0),
            "total": total,
            "agree_pct": round(tally.get("agree", 0) / max(total, 1) * 100),
        }
    except Exception:
        return {"agree": 0, "disagree": 0, "total": 0, "agree_pct": 0}


# ── Topic / Category SEO Pages — Universal (all languages, all countries) ─────

# UI strings per language code (ISO 639-1). Fallback = "en".
_TOPIC_I18N = {
    "en": {"claims": "verified claims", "ai_src": "AI-verified from authoritative sources",
           "no_claims": "No verified claims yet for this topic.",
           "be_first": "Be the first to check a claim", "open": "Open TruthScore",
           "full": "Full analysis →", "checks": "checks", "title_tpl": "Top fact-checked claims about {topic} | TruthScore",
           "desc_tpl": "TruthScore has analyzed {n} claims about {topic} using AI and verified sources. Discover what's true and what's false."},
    "ro": {"claims": "afirmații verificate", "ai_src": "verificate cu AI din surse autorizate",
           "no_claims": "Nu există încă afirmații verificate pentru acest subiect.",
           "be_first": "Verifică prima afirmație", "open": "Deschide TruthScore",
           "full": "Analiză completă →", "checks": "verificări", "title_tpl": "Afirmații verificate despre {topic} | TruthScore",
           "desc_tpl": "TruthScore a analizat {n} afirmații despre {topic} cu AI și surse verificate. Descoperă ce e adevărat și ce e fals."},
    "de": {"claims": "geprüfte Aussagen", "ai_src": "KI-geprüft aus autorisierten Quellen",
           "no_claims": "Noch keine verifizierten Aussagen zu diesem Thema.",
           "be_first": "Erste Aussage prüfen", "open": "TruthScore öffnen",
           "full": "Vollständige Analyse →", "checks": "Prüfungen", "title_tpl": "Faktengeprüfte Aussagen über {topic} | TruthScore",
           "desc_tpl": "TruthScore hat {n} Aussagen über {topic} mit KI analysiert. Entdecke was wahr und was falsch ist."},
    "fr": {"claims": "affirmations vérifiées", "ai_src": "vérifiées par IA depuis des sources fiables",
           "no_claims": "Pas encore d'affirmations vérifiées sur ce sujet.",
           "be_first": "Vérifier la première affirmation", "open": "Ouvrir TruthScore",
           "full": "Analyse complète →", "checks": "vérifications", "title_tpl": "Affirmations vérifiées sur {topic} | TruthScore",
           "desc_tpl": "TruthScore a analysé {n} affirmations sur {topic} avec l'IA. Découvrez ce qui est vrai et ce qui est faux."},
    "es": {"claims": "afirmaciones verificadas", "ai_src": "verificadas con IA desde fuentes autorizadas",
           "no_claims": "Aún no hay afirmaciones verificadas sobre este tema.",
           "be_first": "Verifica la primera afirmación", "open": "Abrir TruthScore",
           "full": "Análisis completo →", "checks": "verificaciones", "title_tpl": "Afirmaciones verificadas sobre {topic} | TruthScore",
           "desc_tpl": "TruthScore ha analizado {n} afirmaciones sobre {topic} con IA. Descubre qué es verdad y qué es falso."},
    "pt": {"claims": "afirmações verificadas", "ai_src": "verificadas com IA de fontes autorizadas",
           "no_claims": "Ainda não há afirmações verificadas sobre este tópico.",
           "be_first": "Verificar a primeira afirmação", "open": "Abrir TruthScore",
           "full": "Análise completa →", "checks": "verificações", "title_tpl": "Afirmações verificadas sobre {topic} | TruthScore",
           "desc_tpl": "TruthScore analisou {n} afirmações sobre {topic} com IA. Descubra o que é verdadeiro e o que é falso."},
    "it": {"claims": "affermazioni verificate", "ai_src": "verificate con IA da fonti autorizzate",
           "no_claims": "Ancora nessuna affermazione verificata su questo argomento.",
           "be_first": "Verifica la prima affermazione", "open": "Apri TruthScore",
           "full": "Analisi completa →", "checks": "verifiche", "title_tpl": "Affermazioni verificate su {topic} | TruthScore",
           "desc_tpl": "TruthScore ha analizzato {n} affermazioni su {topic} con IA. Scopri cosa è vero e cosa è falso."},
    "nl": {"claims": "geverifieerde beweringen", "ai_src": "AI-geverifieerd uit gezaghebbende bronnen",
           "no_claims": "Nog geen geverifieerde beweringen over dit onderwerp.",
           "be_first": "Controleer de eerste bewering", "open": "TruthScore openen",
           "full": "Volledige analyse →", "checks": "controles", "title_tpl": "Geverifieerde beweringen over {topic} | TruthScore",
           "desc_tpl": "TruthScore heeft {n} beweringen over {topic} geanalyseerd met AI."},
    "pl": {"claims": "zweryfikowanych twierdzeń", "ai_src": "zweryfikowane przez AI ze sprawdzonych źródeł",
           "no_claims": "Brak jeszcze zweryfikowanych twierdzeń na ten temat.",
           "be_first": "Sprawdź pierwsze twierdzenie", "open": "Otwórz TruthScore",
           "full": "Pełna analiza →", "checks": "sprawdzeń", "title_tpl": "Zweryfikowane twierdzenia o {topic} | TruthScore",
           "desc_tpl": "TruthScore przeanalizował {n} twierdzeń o {topic} za pomocą AI."},
    "tr": {"claims": "doğrulanmış iddia", "ai_src": "yetkili kaynaklardan yapay zeka ile doğrulandı",
           "no_claims": "Bu konu için henüz doğrulanmış iddia yok.",
           "be_first": "İlk iddiayı kontrol et", "open": "TruthScore'u aç",
           "full": "Tam analiz →", "checks": "kontrol", "title_tpl": "{topic} hakkında doğrulanmış iddialar | TruthScore",
           "desc_tpl": "TruthScore, {topic} hakkında {n} iddiayı yapay zeka ile analiz etti."},
    "ar": {"claims": "ادعاءات موثقة", "ai_src": "تم التحقق منها بالذكاء الاصطناعي من مصادر موثوقة",
           "no_claims": "لا توجد ادعاءات موثقة حول هذا الموضوع بعد.",
           "be_first": "تحقق من أول ادعاء", "open": "افتح TruthScore",
           "full": "تحليل كامل ←", "checks": "تحقق", "title_tpl": "ادعاءات موثقة حول {topic} | TruthScore",
           "desc_tpl": "حلل TruthScore {n} ادعاء حول {topic} باستخدام الذكاء الاصطناعي."},
    "ja": {"claims": "件の検証済み主張", "ai_src": "信頼できる情報源からAIで検証済み",
           "no_claims": "このトピックに関する検証済みの主張はまだありません。",
           "be_first": "最初の主張を確認する", "open": "TruthScoreを開く",
           "full": "完全な分析 →", "checks": "回確認", "title_tpl": "{topic}に関する検証済み主張 | TruthScore",
           "desc_tpl": "TruthScoreはAIを使って{topic}に関する{n}件の主張を分析しました。"},
    "zh": {"claims": "条已验证声明", "ai_src": "通过AI从权威来源验证",
           "no_claims": "该主题目前还没有经过验证的声明。",
           "be_first": "验证第一条声明", "open": "打开TruthScore",
           "full": "完整分析 →", "checks": "次验证", "title_tpl": "关于{topic}的已验证声明 | TruthScore",
           "desc_tpl": "TruthScore使用AI分析了关于{topic}的{n}条声明。"},
    "hi": {"claims": "सत्यापित दावे", "ai_src": "AI द्वारा विश्वसनीय स्रोतों से सत्यापित",
           "no_claims": "इस विषय पर अभी तक कोई सत्यापित दावे नहीं हैं।",
           "be_first": "पहला दावा जांचें", "open": "TruthScore खोलें",
           "full": "पूरा विश्लेषण →", "checks": "जांच", "title_tpl": "{topic} के बारे में सत्यापित दावे | TruthScore",
           "desc_tpl": "TruthScore ने AI का उपयोग करके {topic} के बारे में {n} दावों का विश्लेषण किया।"},
    "ru": {"claims": "проверенных утверждений", "ai_src": "проверено AI из авторитетных источников",
           "no_claims": "Пока нет проверенных утверждений по этой теме.",
           "be_first": "Проверить первое утверждение", "open": "Открыть TruthScore",
           "full": "Полный анализ →", "checks": "проверок", "title_tpl": "Проверенные утверждения о {topic} | TruthScore",
           "desc_tpl": "TruthScore проанализировал {n} утверждений о {topic} с помощью AI."},
    "uk": {"claims": "перевірених тверджень", "ai_src": "перевірено AI з авторитетних джерел",
           "no_claims": "Ще немає перевірених тверджень на цю тему.",
           "be_first": "Перевірити перше твердження", "open": "Відкрити TruthScore",
           "full": "Повний аналіз →", "checks": "перевірок", "title_tpl": "Перевірені твердження про {topic} | TruthScore",
           "desc_tpl": "TruthScore проаналізував {n} тверджень про {topic} за допомогою AI."},
    "ko": {"claims": "개 검증된 주장", "ai_src": "신뢰할 수 있는 출처에서 AI로 검증됨",
           "no_claims": "이 주제에 대한 검증된 주장이 아직 없습니다.",
           "be_first": "첫 번째 주장 확인", "open": "TruthScore 열기",
           "full": "전체 분석 →", "checks": "회 확인", "title_tpl": "{topic}에 관한 검증된 주장 | TruthScore",
           "desc_tpl": "TruthScore는 AI를 사용하여 {topic}에 관한 {n}개의 주장을 분석했습니다."},
}

# RTL languages
_RTL_LANGS = {"ar", "he", "fa", "ur"}

# Country code → default language (for /topic/{country}/{slug} routes)
_COUNTRY_LANG = {
    "ro": "ro", "md": "ro",
    "de": "de", "at": "de", "ch": "de",
    "fr": "fr", "be": "fr", "ca": "fr",
    "es": "es", "mx": "es", "ar": "es", "co": "es",
    "pt": "pt", "br": "pt",
    "it": "it",
    "nl": "nl",
    "pl": "pl",
    "tr": "tr",
    "sa": "ar", "ae": "ar", "eg": "ar",
    "jp": "ja",
    "cn": "zh", "tw": "zh",
    "in": "hi",
    "ru": "ru",
    "ua": "uk",
    "kr": "ko",
    "gb": "en", "us": "en", "au": "en", "nz": "en", "ie": "en",
}


def _detect_lang(request: Request, country_code: str = "") -> str:
    """Detect best language: country_code override > Accept-Language header > en."""
    if country_code and country_code.lower() in _COUNTRY_LANG:
        return _COUNTRY_LANG[country_code.lower()]
    accept = request.headers.get("accept-language", "")
    for part in accept.replace("-", "_").split(","):
        code = part.strip().split(";")[0].split("_")[0].lower()
        if code in _TOPIC_I18N:
            return code
    return "en"


def _t(lang: str, key: str) -> str:
    return _TOPIC_I18N.get(lang, _TOPIC_I18N["en"]).get(key, _TOPIC_I18N["en"].get(key, ""))


async def _render_topic_page(request: Request, topic_slug: str, country_code: str = "") -> HTMLResponse:
    """Universal topic page renderer for any slug, any language, any country."""
    # Sanitize slug → human-readable label (replace hyphens/underscores with spaces, title-case)
    label = topic_slug.replace("-", " ").replace("_", " ").strip()[:80]
    if not label or len(label) < 2:
        raise HTTPException(404, "Invalid topic")

    lang = _detect_lang(request, country_code)
    tr = _TOPIC_I18N.get(lang, _TOPIC_I18N["en"])
    is_rtl = lang in _RTL_LANGS
    dir_attr = 'dir="rtl"' if is_rtl else ''

    try:
        db = get_db()
        # Search by topic field OR keyword match in claim text
        query = {"$or": [
            {"topic": {"$regex": label, "$options": "i"}},
            {"claim": {"$regex": label, "$options": "i"}},
        ]}
        if country_code:
            query["$or"].append({"country": country_code.upper()})  # type: ignore[index]
        docs = await db.trending_claims.find(
            query,
            {"claim": 1, "verdict": 1, "score": 1, "check_count": 1, "_id": 1, "topic": 1}
        ).sort("check_count", -1).limit(40).to_list(40)
    except Exception:
        docs = []

    base = get_public_base_url()
    canonical_path = f"/topic/{country_code.lower()}/{topic_slug}" if country_code else f"/topic/{topic_slug}"

    items_html = ""
    for d in docs:
        v = (d.get("verdict") or "UNCERTAIN").upper()
        color = {"TRUE": "#22c55e", "FALSE": "#ef4444"}.get(v, "#eab308")
        score = d.get("score", 50)
        claim_text = _esc(d.get("claim", "")[:220])
        slug = str(d.get("_id", ""))
        chk = d.get("check_count", 1)
        full_link = f"<a href='{base}/claim/{slug}' style='display:inline-block;margin-top:10px;font-size:12px;color:#a78bfa'>{tr['full']}</a>" if slug else ""
        items_html += f"""<div style="background:#1a1a2e;border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:20px;margin-bottom:12px">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
    <span style="background:{color}22;color:{color};font-size:11px;font-weight:700;padding:3px 8px;border-radius:4px">{v}</span>
    <span style="font-size:12px;color:#6b7280">{score}/100</span>
    <span style="font-size:11px;color:#4b5563;margin-left:auto">✅ {chk} {tr['checks']}</span>
  </div>
  <div style="font-size:15px;color:#e5e7eb;line-height:1.5">{claim_text}</div>
  {full_link}
</div>"""

    count = len(docs)
    title = tr["title_tpl"].format(topic=label.title())
    desc = tr["desc_tpl"].format(n=count, topic=label.title())
    no_claims_html = f'<p style="color:#6b7280">{tr["no_claims"]} <a href="/" style="color:#a78bfa">{tr["be_first"]}</a></p>'

    # Schema.org structured data
    schema = {
        "@context": "https://schema.org", "@type": "CollectionPage",
        "name": title, "url": f"{base}{canonical_path}",
        "description": desc, "inLanguage": lang,
    }
    import json as _json
    schema_str = _json.dumps(schema)

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="{lang}" {dir_attr}>
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(desc)}"/>
<meta property="og:title" content="{_esc(title)}"/>
<meta property="og:description" content="{_esc(desc)}"/>
<meta property="og:url" content="{base}{canonical_path}"/>
<meta property="og:type" content="website"/>
<link rel="canonical" href="{base}{canonical_path}"/>
<script type="application/ld+json">{schema_str}</script>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{background:#0d0d1a;color:#e5e7eb;font-family:'Inter',-apple-system,sans-serif;padding:32px 20px;max-width:780px;margin:0 auto}}</style>
</head>
<body>
<div style="margin-bottom:24px"><a href="/" style="color:#a78bfa;font-size:14px;text-decoration:none">← TruthScore</a></div>
<h1 style="font-size:28px;font-weight:800;color:#fff;margin-bottom:8px">📋 <em style="color:#a78bfa">{_esc(label.title())}</em></h1>
<p style="color:#9ca3af;font-size:15px;margin-bottom:28px">{count} {tr['claims']} — {tr['ai_src']}</p>
{items_html if items_html else no_claims_html}
<div style="margin-top:32px;padding:20px;background:#1a1a2e;border-radius:12px;text-align:center">
  <a href="/" style="background:#6d28d9;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600">{tr['open']}</a>
</div>
</body></html>""")


@app.get("/topic/{country_code}/{topic_slug}", response_class=HTMLResponse, include_in_schema=False)
async def topic_page_country(country_code: str, topic_slug: str, request: Request):
    """Country-specific topic page: /topic/ro/politica, /topic/de/gesundheit, /topic/us/vaccines ..."""
    # Two-letter country code validation
    if len(country_code) != 2 or not country_code.isalpha():
        raise HTTPException(404, "Invalid country code")
    return await _render_topic_page(request, topic_slug, country_code=country_code)


@app.get("/topic/{topic_slug}", response_class=HTMLResponse, include_in_schema=False)
async def topic_page(topic_slug: str, request: Request):
    """Universal topic page: /topic/politics, /topic/health, /topic/vaccins ..."""
    return await _render_topic_page(request, topic_slug)


# ── Pricing page (SEO-indexed) ────────────────────────────────────────────────

@app.get("/pricing", response_class=HTMLResponse, include_in_schema=False)
async def pricing_page():
    base = get_public_base_url()
    plan_rows = ""
    for key in ["free", "pro", "monitor", "business", "enterprise"]:
        p = PLANS.get(key, {})
        price = p.get("price", 0)
        lim = p.get("daily_limit", 0)
        monitors = p.get("features", {}).get("monitors", 0)
        price_str = f"€{price:.2f}/lună" if price > 0 else "Gratuit"
        monitors_str = "Nelimitat" if monitors == -1 else (str(monitors) if monitors > 0 else "—")
        plan_rows += f"<tr><td><b>{p.get('name', key)}</b></td><td>{price_str}</td><td>{'Nelimitat' if lim>=9999 else lim}/zi</td><td>{monitors_str}</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Prețuri TruthScore — Planuri și Abonamente Fact-Checker AI</title>
<meta name="description" content="TruthScore oferă un plan gratuit + planuri Pro, Monitor și Business pentru jurnaliști, companii și cercetători. Vezi comparația completă a planurilor."/>
<meta property="og:title" content="Prețuri TruthScore"/>
<meta property="og:url" content="{base}/pricing"/>
<link rel="canonical" href="{base}/pricing"/>
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"WebPage","name":"Prețuri TruthScore","url":"{base}/pricing"}}</script>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{background:#0d0d1a;color:#e5e7eb;font-family:'Inter',-apple-system,sans-serif;padding:40px 20px;max-width:900px;margin:0 auto}}
h1{{font-size:36px;font-weight:900;text-align:center;margin-bottom:12px}}
.sub{{text-align:center;color:#9ca3af;margin-bottom:40px;font-size:16px}}
table{{width:100%;border-collapse:collapse;background:#1a1a2e;border-radius:12px;overflow:hidden}}
th{{background:#6d28d9;color:#fff;padding:14px 16px;text-align:left;font-size:13px}}
td{{padding:14px 16px;border-bottom:1px solid rgba(255,255,255,.06);font-size:14px}}
tr:last-child td{{border:none}}
.cta{{display:block;text-align:center;margin-top:40px;background:#6d28d9;color:#fff;padding:16px 40px;border-radius:12px;text-decoration:none;font-size:16px;font-weight:700;max-width:300px;margin:40px auto 0}}
</style>
</head>
<body>
<div style="margin-bottom:24px;text-align:center"><a href="/" style="color:#a78bfa;font-size:14px;text-decoration:none">← TruthScore</a></div>
<h1>Prețuri transparente 💎</h1>
<p class="sub">Fără surprize. Poți anula oricând.</p>
<table>
  <thead><tr><th>Plan</th><th>Preț</th><th>Verificări</th><th>Monitoare</th></tr></thead>
  <tbody>{plan_rows}</tbody>
</table>
<ul style="margin-top:32px;padding-left:20px;color:#9ca3af;font-size:14px;line-height:2">
  <li>✅ Toate planurile includ: extensie browser, verificare imagini, istoric personal</li>
  <li>✅ Planul Monitor include alerte prin email când apar claims despre subiectele tale</li>
  <li>✅ Planul Business include API access și până la 20 monitoare</li>
  <li>✅ Trial 7 zile gratuit cu 10 verificări/zi</li>
  <li>🔒 Datele tale sunt private. Nu stocăm claim-urile tale fără acordul tău.</li>
</ul>
<a class="cta" href="/?pricing=1">Alege planul tău</a>
</body></html>""")


@app.get("/today")
async def today_checks():
    """Return today's auto-verified claims from the news scanner."""
    from auth import get_db
    from datetime import datetime, timezone
    db = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    doc = await db["daily_checks"].find_one({"date": today})
    if not doc:
        # Return yesterday as fallback
        yesterday = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0) - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
        doc = await db["daily_checks"].find_one({"date": yesterday})
    if not doc:
        return {"date": today, "items": [], "message": "No checks yet today. Run /news-scanner/run first."}
    doc.pop("_id", None)
    return doc


# ── Bookmarklet ────────────────────────────────────────────────────

@app.get("/bookmarklet.js")
async def bookmarklet():
    """Serve the one-line bookmarklet script."""
    public_url = get_public_base_url()
    js = f"javascript:(function(){{var t=window.getSelection().toString().trim()||document.title;window.open('{public_url}/?claim='+encodeURIComponent(t),'_blank','width=960,height=720,noopener');}})()"
    return PlainTextResponse(js, media_type="application/javascript")


# ── Personal Accuracy Score ────────────────────────────────────────

@app.get("/me/accuracy")
async def my_accuracy(user=Depends(require_user)):
    """Return the user's personal accuracy alignment score."""
    try:
        from auth import get_db
        db = get_db()
        col = db["feedback"]
        fb_docs = await col.find({"user_id": user["id"]}).sort("created_at", -1).to_list(200)
        if not fb_docs:
            return {"checks": 0, "score": None, "badges": [], "message": "Verify more claims to see your accuracy score."}
        # Count how often user's thumbs up/down matched the verdict
        aligned = 0
        total = 0
        for fb in fb_docs:
            user_positive = fb.get("positive")
            verdict = (fb.get("verdict") or "").upper()
            if user_positive is None or not verdict:
                continue
            total += 1
            # Aligned = user said true AND verdict=TRUE, or user said false AND verdict=FALSE
            if (user_positive and verdict == "TRUE") or (not user_positive and verdict == "FALSE"):
                aligned += 1
        score = round(aligned / total * 100) if total > 0 else None
        # Assign badges
        badges = []
        checks = len(fb_docs)
        if checks >= 10:
            badges.append({"id": "fact_hunter", "name": "Fact Hunter", "icon": "🔍", "desc": "Verified 10+ claims"})
        if checks >= 50:
            badges.append({"id": "truth_seeker", "name": "Truth Seeker", "icon": "🏆", "desc": "Verified 50+ claims"})
        if checks >= 100:
            badges.append({"id": "myth_buster", "name": "Myth Buster", "icon": "💥", "desc": "Verified 100+ claims"})
        if score is not None and score >= 80 and total >= 10:
            badges.append({"id": "sharp_eye", "name": "Sharp Eye", "icon": "👁️", "desc": "80%+ accuracy alignment"})
        if score is not None and score >= 95 and total >= 20:
            badges.append({"id": "oracle", "name": "Oracle", "icon": "🔮", "desc": "95%+ accuracy alignment"})
        return {"checks": checks, "aligned": aligned, "total_rated": total, "score": score, "badges": badges}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Twitter/X Bot ──────────────────────────────────────────────────

@app.get("/twitter/webhook")
async def twitter_crc(crc_token: str = ""):
    """Twitter CRC webhook verification."""
    from twitter_bot import verify_crc
    response_token = verify_crc(crc_token)
    return {"response_token": f"sha256={response_token}"}


@app.post("/twitter/webhook")
async def twitter_webhook_handler(request: Request):
    """Receive Twitter Account Activity API events."""
    try:
        body = await request.json()
        tweet_events = body.get("tweet_create_events", [])
        from twitter_bot import handle_mention
        import asyncio as _asyncio
        bot_username = os.getenv("TWITTER_BOT_USERNAME", "TruthScoreBot")
        for tweet in tweet_events:
            if tweet.get("user", {}).get("screen_name", "").lower() == bot_username.lower():
                continue
            _asyncio.create_task(handle_mention(tweet))
    except Exception as e:
        print(f"[twitter-webhook] error: {e}")
    return {"status": "ok"}


@app.post("/twitter/poll-mentions")
async def twitter_poll(user=Depends(require_admin)):
    """Admin: poll recent @mentions and reply."""
    from twitter_bot import poll_and_reply
    return await poll_and_reply()


# ── Slack Bot ──────────────────────────────────────────────────────

@app.post("/slack/command")
async def slack_command(request: Request):
    """Handle Slack slash command /truthcheck."""
    from slack_bot import verify_slack_signature, handle_slash_command
    body_bytes = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "0")
    sig = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(body_bytes, ts, sig):
        raise HTTPException(403, "Invalid Slack signature")
    from urllib.parse import parse_qs
    params = {k: v[0] for k, v in parse_qs(body_bytes.decode()).items()}
    claim = params.get("text", "").strip()
    channel = params.get("channel_id", "")
    import asyncio as _asyncio
    _asyncio.create_task(handle_slash_command(claim, channel))
    return {"response_type": "in_channel", "text": f"⏳ Checking: _{claim[:100]}_…"}


@app.post("/slack/events")
async def slack_events(request: Request):
    """Handle Slack Events API (app_mention, url_verification)."""
    from slack_bot import verify_slack_signature, handle_app_mention
    body_bytes = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "0")
    sig = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(body_bytes, ts, sig):
        raise HTTPException(403, "Invalid Slack signature")
    body = await request.json()
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}
    event = body.get("event", {})
    if event.get("type") == "app_mention":
        import asyncio as _asyncio
        _asyncio.create_task(handle_app_mention(event))
    return {"status": "ok"}


# ── Live Debate Mode ──────────────────────────────────────────────────────────

@app.post("/debate")
async def debate_claim(request: Request, current_user=Depends(get_current_user)):
    """Stream a structured debate between PRO and CON agents on a claim."""
    body = await request.json()
    claim = (body.get("claim") or "").strip()
    if not claim:
        raise HTTPException(status_code=422, detail="claim required")
    if len(claim) > 2000:
        raise HTTPException(status_code=422, detail="claim too long (max 2000 chars)")

    # A debate spins up multiple PRO/CON LLM turns — meter it like a check so it
    # can't be used to bypass the daily quota and rack up cost.
    await enforce_quota(current_user, claim, client_ip=_client_ip(request),
                        fp=_client_fp(request))

    from pipeline.debate import run_debate

    async def event_stream():
        try:
            async for event_json in run_debate(claim):
                yield f"data: {event_json}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Widget ────────────────────────────────────────────────

@app.get("/manifest.json")
async def pwa_manifest():
    return FileResponse(str(Path(__file__).parent / "manifest.json"), media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    return FileResponse(str(Path(__file__).parent / "sw.js"), media_type="application/javascript")

@app.get("/widget.js")
async def widget(user_key: str = ""):
    # Resolve API key → user → plan; widget requires Pro or higher
    if user_key:
        try:
            from auth import get_db
            from utils.api_keys import hash_api_key
            db = get_db()
            doc = await db.api_keys.find_one({"hashed_key": hash_api_key(user_key), "revoked": False})
            if doc:
                u = await db.users.find_one({"_id": __import__("bson").ObjectId(doc["user_id"])})
                plan_name = (u or {}).get("plan", "free")
                from auth import _get_plans
                if not _get_plans().get(plan_name, {}).get("widget", False):
                    from fastapi.responses import PlainTextResponse as _PTR
                    return _PTR("console.error('[TruthScore] Widget requires Pro plan or higher.');", media_type="application/javascript")
        except Exception:
            pass
    return await widget_script(user_key)


# ── Temporal Truth Drift ──────────────────────────────────────────────────────

@app.get("/claims/timeline")
async def get_claim_timeline(claim: str, current_user=Depends(get_current_user)):
    """Get the full truth-over-time history for a claim."""
    from auth import get_db
    from pipeline.temporal_drift import get_drift_summary
    if not claim or len(claim) < 5:
        raise HTTPException(status_code=422, detail="claim required")
    db = get_db()
    summary = await get_drift_summary(db, claim)
    if summary is None:
        return {"has_drift": False, "total_checks": 0, "timeline": []}
    return summary


@app.post("/temporal-drift/scan")
async def run_drift_scan(current_user=Depends(require_admin)):
    """Re-verify all watched claims older than 30 days. Admin only.

    Any claim whose verdict changed fans out a `verdict_change` event to every
    subscribed webhook (this is the one place a verdict genuinely changes).
    """
    from auth import get_db
    from pipeline.temporal_drift import scan_watched_for_drift
    db = get_db()
    drifted = await scan_watched_for_drift(db)

    # Fire verdict_change webhooks for each drifted claim (best-effort).
    if drifted:
        try:
            from api.webhooks import notify_verdict_change
            from pipeline.public_claims import make_slug
            for d in drifted:
                claim = d.get("claim", "")
                verdict_url = f"{PUBLIC_BASE_URL}/claim/{make_slug(claim)}" if claim else PUBLIC_BASE_URL
                await notify_verdict_change(
                    db, claim, d.get("old_verdict", ""), d.get("new_verdict", ""),
                    int(d.get("score", 0)), verdict_url,
                )
        except Exception as e:
            print(f"[WEBHOOKS] verdict_change fan-out skipped (non-fatal): {e}")

    return {"drifted_count": len(drifted), "drifted": drifted[:20]}