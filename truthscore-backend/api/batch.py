"""
TruthScore -- Batch Verify and PDF Export
"""
from config import *
from models import *
from pipeline.verify import verify_claim


async def batch_verify(req: BatchVerifyRequest, user=Depends(require_user)):
    """Verify multiple claims in batch. Pro/Enterprise only."""
    if not AUTH_AVAILABLE:
        raise HTTPException(503, "Auth not configured")
    plan_name = user.get("plan", "free")
    plan = PLANS.get(plan_name, PLANS["free"])
    if plan["batch_limit"] == 0:
        raise HTTPException(403, "Batch verify necesită plan Pro sau Enterprise")
    claims = req.claims[:plan["batch_limit"]]
    results = []
    success = 0
    failed  = 0
    for claim in claims:
        try:
            # Use same pipeline as /verify but without caching individual results
            topic, search_query, claim_en = await smart_detect_topic(claim)
            evidence_tasks, labels = build_source_plan(search_query, topic)
            raw_evidence = await asyncio.gather(*[
                asyncio.wait_for(t, timeout=5.0) for t in evidence_tasks
            ], return_exceptions=True)
            all_evidence = []
            for r in raw_evidence:
                if isinstance(r, list): all_evidence.extend(r)
            top_ev = await rank_by_relevance(claim, all_evidence)[:8]
            score, verdict, confidence, explanation, supporting, contradicting, neutral =                 await reason_with_gpt(claim, top_ev, [])
            results.append({
                "claim": claim, "verdict": verdict, "score": score,
                "confidence": confidence, "explanation": explanation[:200],
                "topic": topic,
                "sources_count": len(supporting) + len(contradicting) + len(neutral),
            })
            success += 1
        except Exception as e:
            results.append({"claim": claim, "verdict": "ERROR", "error": str(e)[:100]})
            failed += 1
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

    # Run verification
    from fastapi.testclient import TestClient
    # Re-use existing verify logic
    verify_req = VerifyRequest(text=req.text)
    # Get result by calling internal logic
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Text gol")

    # Quick verify
    topic, search_query, claim_en = await smart_detect_topic(text)
    t0 = time.time()
    evidence_tasks, labels = build_source_plan(search_query, topic)
    if "DDG_WIKI" not in labels:
        evidence_tasks.append(search_ddg_wiki(search_query))
        labels.append("DDG_WIKI_FALLBACK")
    raw = await asyncio.gather(*[asyncio.wait_for(t, 5.0) for t in evidence_tasks], return_exceptions=True)
    all_ev = []
    for r in raw:
        if isinstance(r, list): all_ev.extend(r)
    top_ev = await rank_by_relevance(text, all_ev)
    score, verdict, confidence, explanation, supporting, contradicting, neutral =         await reason_with_gpt(text, top_ev[:8], [])
    sub_claims  = await split_claims(text)
    word_imp    = compute_word_importance(text, verdict, score)

    result_dict = {
        "claim": text, "verdict": verdict, "score": score,
        "confidence": confidence, "explanation": explanation,
        "topic": topic, "supporting": [s.dict() for s in supporting],
        "contradicting": [s.dict() for s in contradicting],
        "neutral_sources": [s.dict() for s in neutral],
        "evidence_count": len(all_ev),
        "sub_claims": sub_claims,
        "word_importance": word_imp,
        "calibrated_confidence": (
            "Foarte sigur" if confidence=="HIGH" and score>=80 else
            "Sigur" if confidence=="HIGH" else
            "Probabil corect" if confidence=="MEDIUM" and score>=60 else "Incert"
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
