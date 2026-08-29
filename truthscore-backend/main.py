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
import time
import json
import secrets as _secrets
import hashlib as _hashlib
from datetime import datetime, timezone
from typing import Optional as _Optional
from fastapi import Response, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse
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
        logout_user, security as _auth_security,
    )
except ImportError as e:
    print(f"[WARN] Auth not available: {e}")
    AUTH_AVAILABLE = False
    async def register_user(d): raise HTTPException(503, "Auth not configured")
    async def login_user(d):    raise HTTPException(503, "Auth not configured")
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

    # Flip the readiness gate — the process can now serve traffic.
    global _READY
    _READY = True
    print("[STARTUP] Ready to serve.")


@app.on_event("shutdown")
async def _close_pools():
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

    try:
        interaction_id = await log_interaction({
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
        response.headers["X-TruthScore-Interaction-Id"] = interaction_id
    except Exception as e:
        print(f"[CASE-STUDY] Logging skipped (non-fatal): {e}")

    # ── Permanent shareable verdict page (the moat) ──────────────
    # Persist the full result under a short id and hand the client a permalink
    # id. Skip cache hits' re-save is unnecessary — every distinct result gets a
    # stable /v/{id}. Best-effort: a failed save just means no share link.
    try:
        _uid = (user.get("id") or "") if user else ""
        _vid = await save_verdict(result.model_dump(), user_id=_uid)
        if _vid:
            response.headers["X-TruthScore-Verdict-Id"] = _vid
    except Exception as e:
        print(f"[VERDICT-STORE] save skipped (non-fatal): {e}")

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
        task = _asyncio.create_task(verify_claim(req, eco=eco))
        try:
            stages = ["classify", "search", "compare", "score"]
            yield _evt({"stage": stages[0]})
            idx = 1
            # Advance synthetic stages while the pipeline runs; the last stage holds
            # until the real result is ready. Heartbeats keep proxies from buffering.
            while not task.done():
                done, _pending = await _asyncio.wait({task}, timeout=2.5)
                if not done:
                    if idx < len(stages):
                        yield _evt({"stage": stages[idx]}); idx += 1
                    else:
                        yield _evt({"heartbeat": True})
            try:
                result = await task
            except HTTPException as he:
                yield _evt({"event": "error", "detail": str(he.detail), "status": he.status_code}); return
            except Exception as e:
                yield _evt({"event": "error", "detail": str(e)[:200]}); return

            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            try:
                interaction_id = await log_interaction({
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
                interaction_id = ""
                print(f"[CASE-STUDY] Logging skipped (non-fatal): {e}")

            result.models_used = []
            payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
            payload["latency_ms"] = duration_ms
            if interaction_id:
                payload["_interactionId"] = interaction_id
            # Permanent shareable permalink id (moat) — best-effort.
            try:
                _uid = (user.get("id") or "") if user else ""
                _vid = await save_verdict(payload, user_id=_uid)
                if _vid:
                    payload["_verdictId"] = _vid
            except Exception as e:
                print(f"[VERDICT-STORE] save skipped (non-fatal): {e}")
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


_PRIVACY_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy — TruthScore</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Syne:wght@700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#080810;--bg2:#0e0e1a;--bg3:#141424;--bg4:#1a1a2e;
--text:#f0f0fa;--text2:#7878a0;--text3:#3a3a60;
--accent:#5b4eff;--accent-h:#7060ff;--accent2:#8b5cf6;
--border:rgba(255,255,255,.05);--border2:rgba(255,255,255,.09);--border3:rgba(255,255,255,.14)}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,sans-serif;
line-height:1.75;font-size:15px;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;background:
radial-gradient(640px 320px at 12% -6%,rgba(91,78,255,.15),transparent 62%),
radial-gradient(720px 360px at 92% 8%,rgba(139,92,246,.10),transparent 62%)}
.wrap{position:relative;z-index:1;max-width:880px;margin:0 auto;padding:0 22px}
nav{position:sticky;top:0;z-index:20;background:rgba(8,8,16,.84);border-bottom:1px solid var(--border);
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
nav .wrap{display:flex;align-items:center;justify-content:space-between;height:64px}
.brand{display:flex;align-items:center;gap:11px;text-decoration:none;color:var(--text)}
.mark{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;color:#fff;
font-family:'Syne',sans-serif;font-weight:800;font-size:16px;
background:linear-gradient(135deg,var(--accent),var(--accent2));
box-shadow:0 4px 16px rgba(91,78,255,.35)}
.bname{font-family:'Syne',sans-serif;font-weight:700;font-size:17px;letter-spacing:-.3px}
.pill{font-size:13px;font-weight:600;text-decoration:none;color:var(--text2);
border:1px solid var(--border2);padding:8px 16px;border-radius:99px;transition:.18s}
.pill:hover{color:var(--text);border-color:var(--border3);background:rgba(255,255,255,.03)}
header{padding:56px 0 8px}
.badge{display:inline-flex;align-items:center;gap:7px;font-family:'JetBrains Mono',monospace;
font-size:11px;color:var(--accent-h);border:1px solid rgba(91,78,255,.35);
background:rgba(91,78,255,.08);padding:5px 12px;border-radius:99px;margin-bottom:18px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--accent2)}
h1{font-family:'Syne',sans-serif;font-size:clamp(28px,5vw,40px);letter-spacing:-.8px;line-height:1.15}
.sub{color:var(--text2);max-width:660px;margin-top:12px;font-size:15.5px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:34px 0 8px}
.card{background:linear-gradient(180deg,var(--bg2),var(--bg3));border:1px solid var(--border2);
border-radius:14px;padding:18px 18px 15px;transition:.18s}
.card:hover{border-color:var(--border3);transform:translateY(-2px)}
.card .ic{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;font-size:17px;
background:rgba(91,78,255,.12);border:1px solid rgba(91,78,255,.25);margin-bottom:11px}
.card b{display:block;font-size:13.5px;margin-bottom:3px}
.card span{font-size:12.5px;color:var(--text2);line-height:1.55;display:block}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:26px 0 36px}
.toc a{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text2);text-decoration:none;
border:1px solid var(--border2);border-radius:99px;padding:6px 13px;transition:.15s}
.toc a:hover{color:var(--text);border-color:var(--accent);background:rgba(91,78,255,.07)}
h2{font-family:'Syne',sans-serif;font-size:19px;letter-spacing:-.3px}
section{background:linear-gradient(180deg,var(--bg2),var(--bg3));border:1px solid var(--border2);
border-radius:18px;padding:30px 32px;margin:16px 0;scroll-margin-top:84px;transition:.18s}
section:hover{border-color:var(--border3)}
.sh{display:flex;align-items:center;gap:13px;margin-bottom:14px}
.num{width:30px;height:30px;flex-shrink:0;border-radius:9px;display:grid;place-items:center;
font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;color:var(--accent-h);
background:rgba(91,78,255,.10);border:1px solid rgba(91,78,255,.30)}
section p{color:#b8b8d4;margin-top:10px}
section p:first-of-type{margin-top:0}
ul{list-style:none;margin-top:10px}
li{position:relative;padding:5px 0 5px 24px;color:#b8b8d4}
li::before{content:'▸';position:absolute;left:2px;color:var(--accent);font-size:12px}
li b{color:var(--text);font-weight:600}
.vendors{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.vend{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--text2);
border:1px solid var(--border2);background:var(--bg4);border-radius:8px;padding:7px 12px}
.vend em{font-style:normal;color:var(--text)}
.contact{border-radius:18px;padding:1px;margin:32px 0 26px;
background:linear-gradient(135deg,rgba(91,78,255,.55),rgba(139,92,246,.30))}
.contact-in{border-radius:17px;background:linear-gradient(180deg,var(--bg2),var(--bg3));
padding:30px 32px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}
.contact-in h3{font-family:'Syne',sans-serif;font-size:19px}
.contact-in p{color:var(--text2);font-size:13.5px;margin-top:4px}
.cta{white-space:nowrap;text-decoration:none;font-weight:600;font-size:14px;color:#fff;
background:linear-gradient(135deg,var(--accent),var(--accent2));
box-shadow:0 4px 18px rgba(91,78,255,.35);padding:12px 24px;border-radius:11px;transition:.18s}
.cta:hover{filter:brightness(1.12);transform:translateY(-1px)}
footer{border-top:1px solid var(--border);margin-top:10px}
footer .wrap{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;
padding-top:26px;padding-bottom:36px;color:var(--text3);font-size:12.5px}
footer a{color:var(--text2);text-decoration:none}
footer a:hover{color:var(--text)}
@media(max-width:560px){section{padding:24px 20px}}
</style></head><body>

<nav><div class="wrap">
<a class="brand" href="/"><span class="mark">T</span><span class="bname">TruthScore</span></a>
<a class="pill" href="/">← Back to app</a>
</div></nav>

<div class="wrap">
<header>
<span class="badge"><span class="dot"></span>LAST UPDATED · AUGUST 2026</span>
<p class="sub">This policy covers the TruthScore website, dashboard and browser
extension — together, "the Service". We collect the minimum data needed to verify
claims, keep the Service abuse-free and comply with the law. No ads, no tracking
pixels inside the extension.</p>

<div class="cards">
<div class="card"><div class="ic">🔐</div><b>Hashed passwords</b><span>Stored only as bcrypt hashes. Never readable — even by us.</span></div>
<div class="card"><div class="ic">🚫</div><b>No data selling</b><span>We never sell or rent personal data to anyone, ever.</span></div>
<div class="card"><div class="ic">🧩</div><b>Clean extension</b><span>No third-party ads injected into web pages you visit.</span></div>
</div>

<div class="toc">
<a href="#collect">1 · What we collect</a><a href="#thirdparties">2 · Third parties</a>
<a href="#never">3 · What we never do</a><a href="#rights">4 · Your rights</a>
<a href="#contact">5 · Contact</a>
</div>

<section id="collect">
<div class="sh"><span class="num">01</span><h2>What we collect</h2></div>
<ul>
<li><b>Account data:</b> email address, bcrypt-hashed password, display name (optional).</li>
<li><b>Verification inputs:</b> the claims or paragraphs you submit, stored to improve verdict quality, build calibration statistics and prevent abuse.</li>
<li><b>Feedback:</b> thumbs up/down signals you optionally send.</li>
<li><b>Usage counters:</b> daily verification counts and rate-limit identifiers (IP address).</li>
<li><b>Anti-abuse device signal:</b> a browser fingerprint (a hash derived from characteristics such as your browser, screen and a canvas rendering test) used solely to enforce the free-tier daily limit fairly and prevent quota evasion. It is not used for advertising or cross-site tracking.</li>
</ul>
</section>

<section id="thirdparties">
<div class="sh"><span class="num">02</span><h2>Third parties involved in processing</h2></div>
<ul>
<li>Language-model and search providers receive the claim text <b>solely to produce a verdict</b>.</li>
<li>MongoDB Atlas hosts our database; Upstash may host our cache.</li>
<li>Stripe processes payments — <b>card details never reach our servers</b>.</li>
<li>The dashboard may show Google AdSense advertising to free-tier visitors (never in the extension).</li>
</ul>
<div class="vendors">
<span class="vend"><em>Gemini</em> · reasoning</span>
<span class="vend"><em>Groq</em> · inference</span>
<span class="vend"><em>Tavily</em> · search</span>
<span class="vend"><em>MongoDB Atlas</em> · storage</span>
<span class="vend"><em>Upstash</em> · cache</span>
<span class="vend"><em>Stripe</em> · payments</span>
<span class="vend"><em>AdSense</em> · dashboard ads</span>
</div>
</section>

<section id="never">
<div class="sh"><span class="num">03</span><h2>What we never do</h2></div>
<ul>
<li>We never <b>sell or rent personal data</b> — to anyone, under any circumstances.</li>
<li>The browser extension does not inject <b>third-party advertising</b> into web pages.</li>
<li>Passwords are stored only as <b>bcrypt hashes</b> and are never recoverable.</li>
</ul>
</section>

<section id="rights">
<div class="sh"><span class="num">04</span><h2>Your rights</h2></div>
<p>You may request export or deletion of your account and associated logs at any
time. Deletion requests are honored within 30 days and confirmed by email.</p>
<ul>
<li><b>Access &amp; export:</b> receive a machine-readable copy of your data.</li>
<li><b>Rectification:</b> correct inaccurate account details anytime.</li>
<li><b>Erasure:</b> full account removal, including verification logs.</li>
<li><b>Objection:</b> opt out of dashboard advertising by upgrading to any paid plan.</li>
</ul>
</section>

<div class="contact" id="contact"><div class="contact-in">
<div>
<h3>Privacy questions?</h3>
<p>We answer every request within 30 days.</p>
</div>
<a class="cta" href="mailto:privacy@truthscore.app">privacy@truthscore.app</a>
</div></div>

</div><!-- /wrap -->

<footer><div class="wrap">
<span>© 2026 TruthScore · All rights reserved</span>
<span><a href="/">Dashboard</a> · <a href="/privacy">Privacy</a> · <a href="/docs">API docs</a></span>
</div></footer>
</body></html>"""


@app.get("/privacy", include_in_schema=False)
async def privacy_policy():
    """Public privacy-policy page (required by Chrome Web Store & AdSense)."""
    return HTMLResponse(_PRIVACY_HTML)


# ── Auth endpoints ────────────────────────────────────────

@app.post("/auth/register")
async def register(data: UserRegister, request: Request):
    return await register_user(data, client_ip=_client_ip(request))


@app.post("/auth/login")
async def login(data: UserLogin):
    return await login_user(data)


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


# ── API Keys (for widgets, extensions, programmatic access) ──

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


# ── Batch + PDF ───────────────────────────────────────────

@app.post("/batch-verify")
async def batch(req: BatchVerifyRequest, user=Depends(require_user)):
    return await batch_verify(req, user)


class SteelManRequest(BaseModel):
    claim: str = ""
    verdict: str = ""
    score: int = 50


@app.post("/steel-man")
async def steel_man(req: SteelManRequest):
    """Generate the strongest possible counter-argument for a verified claim."""
    claim = (req.claim or "").strip()[:2000]
    if not claim:
        return {"steel_man": "", "key_points": []}
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
        from auth import _get_db
        db = _get_db()
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
        from auth import _get_db
        db = _get_db()
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
        from auth import _get_db
        from bson import ObjectId
        db = _get_db()
        col = db["watched_claims"]
        result = await col.delete_one({"_id": ObjectId(watch_id), "user_id": user["id"]})
        return {"deleted": result.deleted_count > 0}
    except Exception as e:
        raise HTTPException(500, str(e))


async def _resolve_api_key(request) -> dict | None:
    """Check X-API-Key header and return the user dict if valid."""
    api_key = request.headers.get("X-API-Key", "")
    if not api_key.startswith("ts_sk_"):
        return None
    key_hash = _hashlib.sha256(api_key.encode()).hexdigest()
    try:
        from auth import _get_db
        db = _get_db()
        col = db["api_keys"]
        doc = await col.find_one({"key_hash": key_hash, "active": True})
        if not doc:
            return None
        await col.update_one({"_id": doc["_id"]}, {"$inc": {"use_count": 1}, "$set": {"last_used": datetime.now(timezone.utc).isoformat()}})
        return {"id": doc["user_id"], "plan": "pro"}
    except Exception:
        return None


# ── Claim Timeline / Version History ──────────────────────────────

@app.get("/v/{verdict_id}/history")
async def verdict_history(verdict_id: str):
    """Return the full version history for a verdict (how it changed over time)."""
    try:
        from pipeline.verdict_store import load_verdict
        from bson import ObjectId
        from auth import _get_db
        db = _get_db()
        col = db["verdict_history"]
        docs = await col.find({"verdict_id": verdict_id}).sort("checked_at", 1).to_list(50)
        for d in docs:
            d["id"] = str(d.pop("_id", ""))
        # Also return the current verdict
        current = load_verdict(verdict_id)
        return {"verdict_id": verdict_id, "history": docs, "current": current}
    except Exception as e:
        raise HTTPException(500, str(e))


async def _record_verdict_history(verdict_id: str, verdict: str, score: int, claim: str):
    """Append a snapshot to the verdict history collection. Called from /verify."""
    try:
        from auth import _get_db
        db = _get_db()
        col = db["verdict_history"]
        await col.insert_one({
            "verdict_id": verdict_id,
            "verdict": verdict,
            "score": score,
            "claim": claim[:500],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass


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
        from auth import _get_db
        db = _get_db()
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
        backend_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
        from telegram_bot import handle_update
        import asyncio as _asyncio
        _asyncio.create_task(handle_update(update, backend_url))
    except Exception as e:
        print(f"[telegram-webhook] error: {e}")
    return {"ok": True}


# ── Widget ────────────────────────────────────────────────

@app.get("/manifest.json")
async def pwa_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/widget.js")
async def widget(user_key: str = ""):
    return await widget_script(user_key)