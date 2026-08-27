"""
TruthScore v12 -- Main Entry Point
===================================
Thin entry point. All logic lives in submodules.
Run: uvicorn main:app --reload
"""
from config import *
from models import *
from utils.cache import cache, clear_all_caches
from calibration.ece import compute_ece, get_weak_domains, record_feedback, record_feedback_durable, _feedback_store

# Pipeline
from pipeline.verify  import verify_claim
from pipeline.aggregate import aggregate_score
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
        google_auth, google_callback, google_exchange, AUTH_AVAILABLE,
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
        ads_on = (os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
                  and bool(os.getenv("ADSENSE_CLIENT", "").strip()))
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
    # Eco state is a per-user daily flag — capture it from the first check
    # before the per-claim loop below reassigns `quota`.
    eco = bool(quota.get("eco"))

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
    response.headers["X-TruthScore-Show-Ads"] = (
        "1" if os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
        and bool(os.getenv("ADSENSE_CLIENT", "").strip())
        and ((not user) or user.get("plan", "free") == "free") else "0")

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
    )
    try:
        await record_feedback_durable(
            claim          = req.claim,
            verdict        = verdict,
            score          = score,
            topic          = req.topic or "general",
            correct        = correct,
            failure_reason = req.failure_reason or "",
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
        return HTMLResponse(Path("Dashboard.html").read_text(encoding="utf-8"))
    except Exception:
        return HTMLResponse("<h1>TruthScore v12</h1><p>API running.</p>")


@app.get("/tokens.css", include_in_schema=False)
async def tokens_css():
    """Canonical design tokens shared with the extension (kept in sync).
    Referenced by Dashboard.html via <link rel="stylesheet" href="/tokens.css">."""
    from pathlib import Path
    path = Path("tokens.css")
    if path.exists():
        return FileResponse(path, media_type="text/css")
    return PlainTextResponse("", media_type="text/css")


@app.get("/site-config")
async def site_config():
    """Public, non-sensitive frontend configuration."""
    adsense_client = os.getenv("ADSENSE_CLIENT", "").strip()
    ads_flag = os.getenv("ADS_ENABLED", "true").lower() in ("1", "true", "yes")
    return {
        "adsense_client": adsense_client,
        # Ads are only truly enabled when a publisher id is configured;
        # without one there is nothing to serve, so report False in dev.
        "ads_enabled": bool(ads_flag and adsense_client),
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