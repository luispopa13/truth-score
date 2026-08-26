"""
TruthScore v12 -- Main Entry Point
===================================
Thin entry point. All logic lives in submodules.
Run: uvicorn main:app --reload
"""
from config import *
from models import *
from utils.cache import cache, clear_all_caches
from calibration.ece import compute_ece, get_weak_domains, record_feedback, _feedback_store

# Pipeline
from pipeline.verify  import verify_claim
from pipeline.helpers import split_claims

# User case study logging (MSc thesis evaluation data collection) -- new, self-contained
import time
from typing import Optional as _Optional
from fastapi import Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from pipeline.case_study import log_interaction, log_feedback, get_stats, export_merged_csv

# Auth
try:
    from auth import (
        register_user, login_user, get_current_user, require_user,
        check_rate_limit, get_user_out, upgrade_user_plan,
        UserRegister, UserLogin, UserOut, PLANS, create_token,
        google_auth, google_callback, AUTH_AVAILABLE,
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

# CORS: read allow-list from env (default: localhost dev ports)
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS","").split(",") if o.strip()]
if not _origins:
    _origins = [
        "http://localhost:3000", "http://localhost:8000",
        "chrome-extension://*", "moz-extension://*",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins     = _origins,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
    allow_credentials = True,
    # Extension JS must be able to READ these custom response headers
    expose_headers    = [
        "X-TruthScore-Interaction-Id",
        "X-TruthScore-Show-Ads",
        "X-TruthScore-Quota-Left",
        "X-TruthScore-Truncated",
    ],
)


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


# ── Core endpoints ────────────────────────────────────────

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
    rate_info = None
    try:
        # Real client IP (behind proxy/LB too) — feeds the anonymous quota
        client_ip = (request.headers.get("x-forwarded-for", "")
                     .split(",")[0].strip()
                     or (request.client.host if request.client else ""))
        rate_info = await check_rate_limit(user, req.text, client_ip=client_ip)
        if not rate_info["allowed"]:
            from http import HTTPStatus
            if rate_info.get("plan") == "anonymous":
                detail = ("Ai folosit cele 3 verificari gratuite anonime. "
                          "Creeaza un cont gratuit pentru 10/zi + bonusuri.")
            else:
                detail = (f"Limita zilnica de {rate_info['limit']} verificari atinsa. "
                          "Upgrade la Pro pentru mai mult: /app#pricing")
            raise HTTPException(status_code=HTTPStatus.TOO_MANY_REQUESTS, detail=detail)
    except HTTPException:
        raise
    except Exception:
        # Rate-limit must never break the service.
        pass

    start = time.perf_counter()
    result = await verify_claim(req)
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
            "user_email":          user.get("email") if user else None,
            "user_plan":           (user.get("plan") if user else "anonymous"),
        })
        response.headers["X-TruthScore-Interaction-Id"] = interaction_id
    except Exception as e:
        print(f"[CASE-STUDY] Logging skipped (non-fatal): {e}")

    # Internal telemetry records model choices before this point. Public clients
    # receive no provider/model metadata, which also keeps the API contract clean.
    result.models_used = []

    # ── Ad-support flag (free tier monetization) ─────────────────
    # Extension/dashboard reads this header and renders the sponsor slot.
    # Paid plans never see ads.
    try:
        ads_on = os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
        is_free = (not user) or user.get("plan", "free") == "free"
        response.headers["X-TruthScore-Show-Ads"] = (
            "1" if (ads_on and is_free) else "0")
        if rate_info:
            left = max(0, int(rate_info.get("limit", 0)) -
                          int(rate_info.get("used", 0)))
            response.headers["X-TruthScore-Quota-Left"] = str(left)
    except Exception:
        pass

    return result



@app.post("/analyze-text", response_model=TextAnalysisResponse)
async def analyze_text(req: VerifyRequest, response: Response,
                       request: Request,
                       user: dict = Depends(get_current_user)):
    """Extract and independently verify every factual claim in a paragraph."""
    text = req.text.strip()
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or (request.client.host if request.client else ""))

    # Reserve one quota unit before paying for claim extraction.
    quota = await check_rate_limit(user, text, client_ip=client_ip)
    if not quota.get("allowed"):
        raise HTTPException(status_code=429, detail="Daily verification limit reached.")

    claims = await split_claims(text)
    if not claims:
        raise HTTPException(status_code=422, detail="No verifiable factual claims were found.")

    # Every extracted claim is a real verification and consumes one quota unit.
    allowed_claims = [claims[0]]
    for claim in claims[1:]:
        quota = await check_rate_limit(user, claim, client_ip=client_ip)
        if not quota.get("allowed"):
            break
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
            result = await verify_claim(VerifyRequest(text=claim))
            result.models_used = []
            return result

    raw_results = await asyncio.gather(
        *[_verify_one(claim) for claim in allowed_claims],
        return_exceptions=True,
    )
    results = [r for r in raw_results if isinstance(r, VerifyResponse)]
    if not results:
        raise HTTPException(status_code=503, detail="The claims could not be verified right now.")

    verdicts = [r.verdict for r in results]
    true_count = verdicts.count("TRUE")
    false_count = verdicts.count("FALSE")
    uncertain_count = len(results) - true_count - false_count
    mixed = true_count > 0 and false_count > 0
    if mixed:
        verdict = "MIXED"
    elif true_count == len(results):
        verdict = "TRUE"
    elif false_count == len(results):
        verdict = "FALSE"
    else:
        verdict = "UNCERTAIN"

    score = round(sum(r.score for r in results) / len(results))
    confidence = ("LOW" if any(r.confidence == "LOW" for r in results)
                  else "MEDIUM" if any(r.confidence == "MEDIUM" for r in results)
                  else "HIGH")
    explanation = (
        f"Analyzed {len(results)} factual claim(s): {true_count} supported, "
        f"{false_count} contradicted and {uncertain_count} uncertain."
    )
    if quota_left >= 0:
        explanation += (
            f" Consumed {quota_consumed} verification(s); "
            f"{quota_left} left today.")

    response.headers["X-TruthScore-Quota-Left"] = str(quota_left)
    response.headers["X-TruthScore-Show-Ads"] = (
        "1" if os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
        and ((not user) or user.get("plan", "free") == "free") else "0")

    return TextAnalysisResponse(
        text=text,
        verdict=verdict,
        score=score,
        confidence=confidence,
        explanation=explanation,
        results=results,
        claim_count=len(results),
        mixed=mixed,
        quota_consumed=quota_consumed,
        quota_left=quota_left,
    )


@app.post("/detect-claims")
async def detect_claims(req: ClaimDetectRequest, request: Request,
                        user: dict = Depends(get_current_user)):
    """Claim-splitting preview. Rate-limited because it invokes an LLM call."""
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or (request.client.host if request.client else ""))
    try:
        info = await check_rate_limit(user, req.text, client_ip=client_ip)
        if info and not info.get("allowed"):
            raise HTTPException(status_code=429, detail="Daily verification limit reached.")
    except HTTPException:
        raise
    except Exception:
        pass  # limiter must never break detection
    claims = await split_claims(req.text)
    return {"claims": claims, "count": len(claims)}


@app.post("/feedback")
async def feedback(req: FeedbackRequest, user: dict = Depends(get_current_user)):
    # Resolve either naming convention: the dashboard sends
    # verdict/score/correct, the browser extension sends
    # predicted_verdict/predicted_score/user_says_correct.
    verdict = req.verdict or req.predicted_verdict or "UNCERTAIN"
    score   = req.score if req.score else (req.predicted_score or 50)
    correct = req.user_says_correct if req.user_says_correct is not None else req.correct

    record_feedback(
        claim          = req.claim,
        verdict        = verdict,
        score          = score,
        topic          = req.topic or "general",
        correct        = correct,
        failure_reason = req.failure_reason or "",
    )

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


# ── User Case Study (MSc thesis evaluation data collection) ──────

class CaseStudyFeedback(BaseModel):
    """Self-contained model -- does not depend on models.py."""
    interaction_id: str
    correct: bool
    comment: _Optional[str] = None
    user_id: _Optional[str] = None


@app.post("/case-study/feedback")
async def case_study_feedback(req: CaseStudyFeedback):
    """
    Links user feedback (thumbs up/down) to a specific logged interaction
    via the interaction_id returned in the X-TruthScore-Interaction-Id
    header of the original /verify response.
    """
    await log_feedback(req.interaction_id, req.correct, req.comment, req.user_id)
    return {"status": "recorded", "interaction_id": req.interaction_id}


@app.get("/case-study/stats")
async def case_study_stats():
    """Quick summary of collected case-study data (counts, latency, agreement)."""
    return await get_stats()


@app.get("/case-study/export")
async def case_study_export():
    """
    Downloads a single merged CSV (interactions + linked feedback),
    ready to open in Excel or load with pandas for the thesis
    evaluation chapter.
    """
    path = await export_merged_csv()
    return FileResponse(
        path,
        media_type="text/csv",
        filename="truthscore_case_study.csv",
    )


@app.get("/ece")
async def ece(dataset: str = "results_truthfulqa_mini.csv"):
    if not os.path.exists(dataset):
        return {"error": f"File not found: {dataset}"}
    return compute_ece(dataset)


@app.get("/weak-domains")
async def weak_domains():
    return {
        "weak_domains":   get_weak_domains(),
        "feedback_count": len(_feedback_store),
    }


@app.post("/clear-cache")
async def clear_cache_endpoint():
    """Clear all cached verify results."""
    result = clear_all_caches()
    print(f"[CACHE] Cleared {result['cleared']} entries from {result['cache_dir']}")
    return {
        "status":  "cleared",
        "entries": result["cleared"],
        "dir":     result["cache_dir"],
    }


@app.get("/cache-stats")
async def cache_stats():
    """Show cache statistics."""
    return {
        "entries":   len(cache),
        "cache_dir": str(cache.directory),
        "size_mb":   round(sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, dn, fn in os.walk(cache.directory)
            for f in fn
        ) / 1024 / 1024, 2),
    }


@app.get("/health")
async def health():
    import config as _cfg
    groq_key = os.getenv("GROQ_API_KEY", "")
    return {
        "status":  "ok",
        "version": "12.0",
        "gemini":  f"set ({GEMINI_MODEL})" if GEMINI_API_KEY else "MISSING",
        "groq":    "set" if groq_key else "missing",
        "tavily":  "set" if TAVILY_API_KEY else "missing",
        "auth":    "available" if AUTH_AVAILABLE else "stub",
        "cache":   f"{len(cache)} entries",
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


@app.get("/")
async def root():
    from fastapi.responses import HTMLResponse
    from pathlib import Path
    try:
        return HTMLResponse(Path("dashboard.html").read_text(encoding="utf-8"))
    except Exception:
        return HTMLResponse("<h1>TruthScore v12</h1><p>API running.</p>")


@app.get("/site-config")
async def site_config():
    """Public, non-sensitive frontend configuration."""
    return {
        "adsense_client": os.getenv("ADSENSE_CLIENT", "").strip(),
        "ads_enabled": os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes"),
    }


@app.get("/ads.txt", include_in_schema=False)
async def ads_txt():
    """AdSense authorization file — auto-generated from ADSENSE_CLIENT env."""
    pub = os.getenv("ADSENSE_CLIENT", "").strip().removeprefix("ca-pub-")
    if not pub:
        return PlainTextResponse("")
    return PlainTextResponse(
        f"google.com, pub-{pub}, DIRECT, f08c47fec0942fa0\n")


_PRIVACY_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TruthScore — Privacy Policy</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222}
h1{font-size:26px}h2{font-size:18px;margin-top:28px}a{color:#5b6cff}</style></head><body>
<h1>TruthScore — Privacy Policy</h1>
<p>Last updated: August 2026. This policy covers the TruthScore website, dashboard
and browser extension ("the Service").</p>

<h2>1. What we collect</h2>
<ul>
<li><b>Account data:</b> email address, bcrypt-hashed password, display name (optional).</li>
<li><b>Verification inputs:</b> the claims or paragraphs you submit, stored to improve
verdict quality, build calibration statistics and prevent abuse.</li>
<li><b>Feedback:</b> thumbs up/down signals you optionally send.</li>
<li><b>Usage counters:</b> daily verification counts, rate-limit identifiers (IP address).</li>
</ul>

<h2>2. Third parties involved in processing</h2>
<ul>
<li>Large language model and search providers (Google Gemini, Groq, Tavily) receive the
claim text solely to produce a verdict.</li>
<li>MongoDB Atlas hosts our database; Upstash may host our cache.</li>
<li>Stripe processes payments — card details never reach our servers.</li>
<li>The dashboard may show Google AdSense advertising to free-tier visitors; see Google's
own privacy policy.</li>
</ul>

<h2>3. What we never do</h2>
<ul>
<li>We never sell personal data.</li>
<li>The browser extension does not inject third-party advertising into web pages.</li>
<li>Passwords are stored only as bcrypt hashes.</li>
</ul>

<h2>4. Your rights</h2>
<p>You may request export or deletion of your account and associated logs at any time by
contacting us. Deletion requests are honored within 30 days.</p>

<h2>5. Contact</h2>
<p>Privacy inquiries: <b>TODO: your contact email</b></p>
<p><a href="/">← Back to TruthScore</a></p>
</body></html>"""


@app.get("/privacy", include_in_schema=False)
async def privacy_policy():
    """Public privacy-policy page (required by Chrome Web Store & AdSense)."""
    return HTMLResponse(_PRIVACY_HTML)


# ── Auth endpoints ────────────────────────────────────────

@app.post("/auth/register")
async def register(data: UserRegister, request: Request):
    client_ip = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()   # behind proxy/LB
    return await register_user(data, client_ip=client_ip)


@app.post("/auth/login")
async def login(data: UserLogin):
    return await login_user(data)


@app.get("/auth/me")
async def me(user=Depends(require_user)):
    return await get_user_out(user)


@app.post("/auth/logout")
async def logout():
    return {"status": "ok"}


@app.post("/auth/google")
async def auth_google(req: GoogleAuthRequest):
    return await google_auth(req)


@app.get("/auth/google/callback")
async def auth_google_callback():
    return await google_callback()


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
    from utils.api_keys import create_api_key
    plan = user.get("plan", "free")
    result = await create_api_key(str(user["_id"]), plan=plan)
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

@app.get("/metrics/cost")
async def cost_metrics():
    """Real-time cost tracking (USD spent, per model)."""
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
async def quota_metrics():
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


@app.post("/verify-pdf")
async def pdf(req: VerifyRequest, user=Depends(require_user)):
    return await verify_and_pdf(req, user)


# ── Widget ────────────────────────────────────────────────

@app.get("/widget.js")
async def widget(user_key: str = ""):
    return await widget_script(user_key)