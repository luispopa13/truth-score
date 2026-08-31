"""
TruthScore -- Batch Verify and PDF Export
"""
import time
from config import *
from models import *
from pipeline.verify import verify_claim, retrieve_and_rank
from pipeline.reasoning import reason_with_gpt
from pipeline.helpers import split_claims, compute_word_importance


async def batch_verify(req: BatchVerifyRequest, user=Depends(require_user)):
    """Verify multiple claims in batch. Pro/Enterprise only."""
    if not AUTH_AVAILABLE:
        raise HTTPException(503, "Auth not configured")
    plan_name = user.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS["free"])
    if plan["batch_limit"] == 0:
        raise HTTPException(403, "Batch verify necesită plan Pro sau Enterprise")
    claims = req.claims[:plan["batch_limit"]]

    # Bound concurrency so a large batch can't fan out into hundreds of parallel
    # provider calls (and 429s); claims within the batch still run in parallel.
    sem = asyncio.Semaphore(int(os.getenv("BATCH_CONCURRENCY", "4")))

    async def _verify_one(claim: str) -> dict:
        async with sem:
            try:
                # Shared retrieval+rank pipeline (same as /verify): per-source
                # budgets, dedup, counter-evidence, embedding + cross-encoder.
                rr = await retrieve_and_rank(claim)
                score, verdict, confidence, explanation, supporting, contradicting, neutral, _correct = \
                    await reason_with_gpt(claim, rr.top_k, rr.rest)
                return {
                    "claim": claim, "verdict": verdict, "score": score,
                    "confidence": confidence, "explanation": explanation[:200],
                    "topic": rr.topic,
                    "sources_count": len(supporting) + len(contradicting) + len(neutral),
                }
            except Exception as e:
                # Must satisfy VerifyResponse's required fields (score/confidence/
                # explanation) or BatchVerifyResponse validation rejects the whole
                # batch; surface the error text via explanation.
                return {"claim": claim, "verdict": "ERROR", "score": 0,
                        "confidence": "LOW",
                        "explanation": f"Verification failed: {str(e)[:180]}"}

    results = await asyncio.gather(*[_verify_one(c) for c in claims])
    success = sum(1 for r in results if r.get("verdict") != "ERROR")
    failed  = len(results) - success
    return BatchVerifyResponse(results=results, total=len(claims), success=success, failed=failed)


# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]
# PDF REPORT
# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]


async def verify_and_pdf(req: VerifyRequest, user=Depends(require_user)):
    """Verify claim and return PDF report. Pro/Enterprise only."""
    from fastapi.responses import Response as FResponse
    if not AUTH_AVAILABLE:
        raise HTTPException(503, "Auth not configured")
    if not PDF_AVAILABLE:
        raise HTTPException(503, "PDF generation not available. Install: pip install reportlab")
    plan_name = user.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS["free"])
    if not plan["pdf"]:
        raise HTTPException(403, "Raportul PDF necesită plan Pro sau Enterprise")

    # Run verification (reuses the shared pipeline helpers below)
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Text gol")

    # Quick verify — shared retrieval+rank pipeline (same as /verify).
    t0 = time.time()
    rr = await retrieve_and_rank(text)
    topic = rr.topic
    score, verdict, confidence, explanation, supporting, contradicting, neutral, correct_answer = \
        await reason_with_gpt(text, rr.top_k, rr.rest)
    sub_claims  = await split_claims(text)
    word_imp    = compute_word_importance(text, verdict, score)

    result_dict = {
        "claim": text, "verdict": verdict, "score": score,
        "confidence": confidence, "explanation": explanation,
        "correct_answer": correct_answer,
        "topic": topic, "supporting": [s.dict() for s in supporting],
        "contradicting": [s.dict() for s in contradicting],
        "neutral_sources": [s.dict() for s in neutral],
        "evidence_count": len(rr.all_evidence),
        "sub_claims": sub_claims,
        "word_importance": word_imp,
        # PDF is a static document with no client-side i18n, so emit readable
        # English text directly (EN-default) rather than an enum key.
        "calibrated_confidence": (
            "Very confident" if confidence=="HIGH" and score>=80 else
            "Confident" if confidence=="HIGH" else
            "Likely correct" if confidence=="MEDIUM" and score>=60 else "Uncertain"
        ),
        "latency": {"total_ms": round((time.time()-t0)*1000)},
    }

    pdf_bytes = generate_pdf_report(result_dict)
    filename  = f"truthscore_{verdict.lower()}_{score}.pdf"
    return FResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
