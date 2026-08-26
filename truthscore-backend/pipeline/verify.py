"""
TruthScore — Main verification pipeline.
"""
from config import *
from models import *
from utils.cache import cache
from pipeline.retrieval import *
from pipeline.ranking import rank_by_relevance, rerank_with_crossencoder
from pipeline.reasoning import (
    call_llm_raw, reason_with_gpt, reason_path_b,
    _path_b_triggers, PATH_B_WEIGHTS, pick_model,
)
from pipeline.source_plan import build_source_plan, DOMAIN_SOURCES, search_counter_evidence
from pipeline.decomposition import (
    hyde_generate_queries, hyde_retrieve,
    generate_targeted_queries, search_with_queries,
    factscore_verify, averitec_verify, wikidata_sparql_verify,
)
from pipeline.helpers import (
    normalize_claim, is_temporal_claim, is_nuance_claim, is_strict_domain,
    get_source_recency_weight, build_nuance_queries,
    split_claims, compute_word_importance, smart_detect_topic,
    detect_topic, evaluate_math_claim, extract_keywords,
    _domain, factcheck_rating_to_nli,
)

async def verify_claim(req: VerifyRequest):
    import time as _t
    claim = req.text.strip()
    key   = f"v3:{normalize_claim(claim)}"   # normalized cache key
    t_total_start = _t.time()

    # ── Distributed semantic cache (exact + near-duplicate) ───
    # Cache hits are FREE — no LLM, no evidence fetch, no rate-limit cost.
    try:
        from utils.semantic_cache import semantic_lookup, semantic_store
        hit = await semantic_lookup(claim)
        if hit:
            hit["cached"] = True
            print(f"  [CACHE] HIT (semantic) for '{claim[:50]}'")
            return VerifyResponse(**hit)
    except Exception:
        # Fall back to legacy diskcache
        hit = cache.get(key)
        if hit:
            hit["cached"] = True
            return VerifyResponse(**hit)

    # ── Math shortcut ─────────────────────────────────────────
    math_result = evaluate_math_claim(claim)
    if math_result:
        score, expl = math_result
        verdict    = "TRUE" if score >= 62 else ("FALSE" if score < 38 else "UNCERTAIN")
        result = VerifyResponse(
            claim=claim, score=score, verdict=verdict,
            confidence="HIGH", explanation=expl,
            models_used=["mathematical-evaluator"],
        )
        cache.set(key, result.model_dump(), expire=3600 * 24)
        return result

    # ── Step 1: Evidence Retrieval — all LLM pre-steps IN PARALLEL ──
    # topic + HyDE + targeted queries run concurrently (was: serial topic
    # first) -> saves one full LLM round-trip (~0.7-1.5s) on every claim.
    t1 = _t.time()
    topic_task   = smart_detect_topic(claim)
    hyde_task    = hyde_generate_queries(claim)
    targeted_task = generate_targeted_queries(claim)
    topic, search_query, claim_en = await topic_task
    print(f"\n[PIPELINE] Claim: {claim[:80]}... | Topic: {topic} | Query: {search_query}")

    # Domain-routed evidence retrieval starts immediately after topic
    evidence_tasks, labels = build_source_plan(search_query, topic)

    # Await HyDE and targeted queries (already running since above)
    hyde_data, targeted_queries = await asyncio.gather(hyde_task, targeted_task)

    # Add HyDE retrieval to pipeline
    evidence_tasks.append(hyde_retrieve(claim, hyde_data))
    labels.append("HYDE")
    # ── FREE-FIRST SEARCH CASCADE (cost control) ─────────────────
    # Tavily costs $0.008/credit — it must NOT fire on every claim.
    # Modes: "always" (old behavior) | "fallback_only" (default, recommended)
    #        | "off" (free sources only)
    TAVILY_MODE = os.getenv("TAVILY_MODE", "fallback_only")
    if ("TAVILY" not in labels and TAVILY_API_KEY
            and TAVILY_MODE == "always"):
        evidence_tasks.append(search_tavily(search_query))
        labels.append("TAVILY_FALLBACK")
    # ALWAYS add counter-evidence search -- finds debunking for tricky claims
    evidence_tasks.append(search_counter_evidence(claim))
    labels.append("COUNTER_EVIDENCE")

    # Add targeted query searches (support + contradict + consensus)
    # These run alongside domain retrieval for balanced evidence
    if targeted_queries:
        targeted_sources_task = search_with_queries(targeted_queries)
        evidence_tasks.append(targeted_sources_task)
        labels.append("TARGETED_QUERIES")

    # ── Fix 1: Nuance claims get extra exception/counter search ──
    # "Space is completely silent" -> search for "space not silent exceptions"
    if is_nuance_claim(claim):
        print(f"  [NUANCE] Absolute language detected -> adding exception search")
        nuance_queries = build_nuance_queries(claim)
        evidence_tasks.append(search_ddg_wiki(nuance_queries[0]))
        labels.append("NUANCE_EXCEPTION")
        if TAVILY_API_KEY and TAVILY_MODE == "always":
            evidence_tasks.append(search_tavily(nuance_queries[1]))
            labels.append("NUANCE_TAVILY")

    # ── Fix 2: Strict domain -> force PubMed systematic reviews ──
    # Medical/biological claims need peer-reviewed sources, not just web
    if is_strict_domain(claim, topic):
        print(f"  [STRICT] Medical/scientific claim -> boosting peer-reviewed sources")
        if "PUBMED" not in labels:
            evidence_tasks.append(search_pubmed(claim))
            labels.append("PUBMED_STRICT")
        if "SEMANTIC_SCHOLAR" not in labels:
            evidence_tasks.append(search_semantic_scholar(claim + " systematic review meta-analysis"))
            labels.append("SEMANTIC_STRICT")

    # Always add broad web search as secondary fallback
    if "DDG_WIKI" not in labels:
        evidence_tasks.append(search_ddg_wiki(search_query))
        labels.append("DDG_BROAD_FALLBACK")
    # Always add CrossRef as academic fallback
    if "CROSSREF" not in labels:
        evidence_tasks.append(search_crossref(search_query))
        labels.append("CROSSREF_FALLBACK")
    # Always add Semantic Scholar as second academic fallback
    if "SEMANTIC_SCHOLAR" not in labels:
        evidence_tasks.append(search_semantic_scholar(search_query))
        labels.append("SEMANTIC_FALLBACK")
    # Cap each source at 5 seconds -- slow sources get skipped
    async def _with_timeout(coro, label):
        try:
            return await asyncio.wait_for(coro, timeout=5.0)
        except asyncio.TimeoutError:
            print(f"  [{label}] TIMEOUT (>5s)")
            return []
        except Exception as e:
            return e

    results = await asyncio.gather(
        *[_with_timeout(t, l) for t, l in zip(evidence_tasks, labels)],
        return_exceptions=True
    )
    t_retrieval = (_t.time() - t1) * 1000

    all_evidence: list[Source] = []
    for label, res in zip(labels, results):
        if isinstance(res, Exception):
            print(f"  [{label}] ERROR: {str(res)[:100]}")
        else:
            print(f"  [{label}] {len(res)} sources")
            all_evidence.extend(res)

    # ── Deduplicate by URL (same source from multiple APIs) ──────
    seen_urls: set = set()
    deduped: list[Source] = []
    for src in all_evidence:
        url_key = src.url.rstrip("/").lower().split("?")[0]  # strip query params
        if url_key and url_key not in seen_urls:
            seen_urls.add(url_key)
            deduped.append(src)
        elif not url_key:
            deduped.append(src)  # keep sources with no URL
    removed = len(all_evidence) - len(deduped)
    if removed:
        print(f"  [DEDUP] Removed {removed} duplicate URLs")
    all_evidence = deduped

    # ── PAID-SEARCH TOP-UP (fallback_only mode) ───────────────────
    # Free sources came back thin → now (and only now) spend on Tavily.
    # Most claims never reach this line, cutting search cost ~70%.
    MIN_EVIDENCE = int(os.getenv("MIN_FREE_EVIDENCE", "6"))
    if (TAVILY_MODE == "fallback_only" and TAVILY_API_KEY
            and "TAVILY_FALLBACK" not in labels
            and len(all_evidence) < MIN_EVIDENCE):
        print(f"  [TAVILY] Free evidence thin ({len(all_evidence)}) -> paid top-up")
        try:
            tavily_results = await asyncio.wait_for(
                search_tavily(search_query), timeout=8.0)
            print(f"  [TAVILY-TOPUP] +{len(tavily_results)} sources")
            all_evidence.extend(tavily_results)
            models_used_extra = ["tavily-topup"]
        except Exception as e:
            print(f"  [TAVILY-TOPUP] failed: {e}")

    print(f"  TOTAL: {len(all_evidence)} evidence | retrieval: {t_retrieval:.0f}ms")

    # ── Step 2: Two-stage relevance ranking ──────────────────────
    # Stage A: Embedding cosine similarity (fast, high recall) -> top 30
    # Stage B: Cross-encoder reranking (slower, high precision) -> top 12
    models_used = []
    t2 = _t.time()

    # Stage A -- embedding filter: many -> 30
    EMBED_BROAD_K = min(30, len(all_evidence))
    ranked_broad = await rank_by_relevance(claim, all_evidence)
    candidates   = ranked_broad[:EMBED_BROAD_K]
    rest         = ranked_broad[EMBED_BROAD_K:]

    # Stage B -- cross-encoder rerank: 30 -> 12
    top_k = await rerank_with_crossencoder(claim, candidates, top_k=EMBED_TOP_K)

    # Update relevance scores for display
    for i, src in enumerate(top_k):
        if src.relevance == 0.0:  # not yet set by cross-encoder
            src.relevance = round(1.0 - i / max(len(top_k), 1), 3)

    t_embedding = (_t.time() - t2) * 1000
    print(f"  [RANK] {len(all_evidence)} -> embed top {EMBED_BROAD_K}"
          f" -> cross-encoder top {len(top_k)} | {t_embedding:.0f}ms")

    # ── Step 3: Path A — Gemini reasoning ────────────────────
    t3 = _t.time()
    if gemini_client and top_k:
        # ── MODEL ROUTING with quality-escalation ──────────────
        # 1. pick_model(): easy short claims -> cheap Groq GPT-OSS;
        #    hard signals -> straight to Gemini.
        # 2. If the cheap result comes back weak (LOW/UNCERTAIN), re-run
        #    ONCE with Gemini — users never see degraded verdicts; extra
        #    latency lands only on genuinely hard claims.
        claim_for_llm = claim_en if claim_en != claim else claim
        chosen_model = pick_model(claim, topic)

        score, verdict, confidence, explanation, supporting, contradicting, neutral = \
            await reason_with_gpt(claim_for_llm, top_k, rest, model_hint=chosen_model)
        models_used.append(chosen_model)

        if chosen_model not in ("gemini",) and (verdict == "UNCERTAIN" or confidence == "LOW"):
            print(f"  [ROUTE] cheap result weak ({verdict}/{confidence}) -> escalating to Gemini")
            score, verdict, confidence, explanation, supporting, contradicting, neutral = \
                await reason_with_gpt(claim_for_llm, top_k, rest, model_hint="gemini")
            models_used.append("gemini-escalation")

        scored = top_k + rest

        # ── Path B — Evidence-based cross-check ──────────────
        # Runs only when Path A result is ambiguous or claim is high-risk.
        # Path B classifies stance of each source mathematically —
        # no LLM parametric memory involved in the final verdict.
        if _path_b_triggers(score, verdict, claim, topic):
            print(f"  [PATH-B] Triggered (score={score}, nuance={is_nuance_claim(claim)})")
            b_score, b_verdict, b_conf, b_expl = await reason_path_b(claim, top_k)

            if b_score is not None and b_verdict is not None:
                a_distance       = abs(score - 50)
                b_distance       = abs(b_score - 50)
                nuance_or_strict = is_nuance_claim(claim) or is_strict_domain(claim, topic)

                if verdict != b_verdict:
                    if nuance_or_strict or b_distance > a_distance + 10:
                        print(f"  [PATH-B] Override: A={verdict}({score}) "
                              f"-> B={b_verdict}({b_score}) "
                              f"({'evidence-priority' if nuance_or_strict else 'more decisive'})")
                        score       = b_score
                        verdict     = b_verdict
                        confidence  = b_conf
                        explanation = (f"[Evidence-based] {b_expl} "
                                      f"(Initial assessment was {verdict})")
                        models_used.append("evidence-stance-classifier")
                    else:
                        print(f"  [PATH-B] Conflict -> UNCERTAIN")
                        score       = 50
                        verdict     = "UNCERTAIN"
                        confidence  = "LOW"
                        explanation = (f"Conflicting evidence: sources suggest "
                                      f"{b_verdict} but overall assessment "
                                      f"suggested {verdict}. {b_expl}")
                else:
                    avg_score = (score + b_score) // 2
                    score     = avg_score
                    if confidence == "LOW":
                        confidence = "MEDIUM"
                    print(f"  [PATH-B] Both agree: {verdict} -> confidence boosted")

        # ── Luna 2: FActScore atomic decomposition ────────────
        # Decompose compound claims into atomic facts, verify each.
        # Catches partial truths where one sub-claim is false.
        # LATENCY GATE: only on genuinely doubtful results — running it on
        # every medium-confidence claim added a full LLM round-trip to the
        # median response for marginal accuracy gain.
        if confidence != "HIGH" or is_nuance_claim(claim) or is_strict_domain(claim, topic):
            print(f"  [FACTSCORE] Running (score={score})")
            fs_score, fs_verdict, fs_conf, fs_expl, _ = \
                await factscore_verify(claim, top_k)
            if fs_score is not None and fs_verdict is not None:
                fs_dist  = abs(fs_score - 50)
                cur_dist = abs(score - 50)
                if fs_dist > cur_dist + 5:
                    print(f"  [FACTSCORE] Override: {verdict}({score}) "
                          f"-> {fs_verdict}({fs_score})")
                    score, verdict, confidence, explanation = \
                        fs_score, fs_verdict, fs_conf, fs_expl
                    models_used.append("factscore-decomposition")
                elif fs_verdict != verdict and fs_dist > 10:
                    score, verdict, confidence = 50, "UNCERTAIN", "LOW"
                    explanation = f"Atomic analysis conflicts. {fs_expl}"

        # ── Luna 3: AVeriTeC question decomposition ───────────
        # Generate verification questions, answer each with retrieval.
        # Resolves UNCERTAIN verdicts through targeted Q&A.
        if verdict == "UNCERTAIN":
            print(f"  [AVERITEC] Running question decomposition")
            av_score, av_verdict, av_conf, av_expl = \
                await averitec_verify(claim)
            if av_score is not None and av_verdict in ("TRUE", "FALSE"):
                print(f"  [AVERITEC] Resolved: {av_verdict}({av_score})")
                score, verdict, confidence, explanation = \
                    av_score, av_verdict, av_conf, av_expl
                models_used.append("averitec-qa")

        # ── Luna 4: Wikidata SPARQL for structured facts ──────
        # Verifies geographic, demographic, historical facts via
        # structured SPARQL queries on Wikidata. No API key needed.
        if topic in ("geography", "history", "politics", "science"):
            wikidata_srcs = await wikidata_sparql_verify(claim)
            if wikidata_srcs:
                top_k = wikidata_srcs + top_k
                print(f"  [WIKIDATA] Added {len(wikidata_srcs)} structured sources")
    elif all_evidence:
        # No Gemini configured — evidence collected but no reasoner available.
        scored = all_evidence
        score, verdict, confidence = 50, "UNCERTAIN", "LOW"
        explanation = "Modelul AI nu este configurat. Adaugă GEMINI_API_KEY în .env"
        supporting, contradicting, neutral = [], [], scored[:3]
    else:
        scored = all_evidence
        score, verdict, confidence = 50, "UNCERTAIN", "LOW"
        explanation = "Modelul AI nu este configurat. Adaugă GEMINI_API_KEY în .env"
        supporting, contradicting, neutral = [], [], scored[:3]
    t_nli = (_t.time() - t3) * 1000
    t_aggregation = 0.0
    t_total = (_t.time() - t_total_start) * 1000
    print(f"  [REASON] {t_nli:.0f}ms | verdict={verdict} score={score}")

    src_per_sec = round(len(all_evidence) / (t_total / 1000), 1) if t_total > 0 else 0

    print(f"  [LATENCY] total={t_total:.0f}ms | retrieval={t_retrieval:.0f}ms"
          f" | embed={t_embedding:.0f}ms | nli={t_nli:.0f}ms"
          f" | agg={t_aggregation:.0f}ms | {src_per_sec} src/s")

    latency = LatencyBreakdown(
        total_ms=round(t_total, 1),
        retrieval_ms=round(t_retrieval, 1),
        embedding_ms=round(t_embedding, 1),
        nli_ms=round(t_nli, 1),
        aggregation_ms=round(t_aggregation, 1),
        sources_per_second=src_per_sec,
    )

    # Claim splitting & explainability (runs fast, parallel with cache write)
    sub_claims      = await split_claims(claim)
    word_importance = compute_word_importance(claim, verdict, score)

    # ── Real confidence calibration ───────────────────────────
    # Based on: score distance from 50, source quality, agreement
    score_distance = abs(score - 50)          # 0=uncertain, 50=certain
    n_factcheck    = sum(1 for s in supporting + contradicting
                        if s.type == "factcheck")
    n_academic     = sum(1 for s in supporting + contradicting
                        if s.type == "academic")
    n_relevant     = len(supporting) + len(contradicting)

    # Recalculate confidence from scratch -- ignore Gemini's self-assessment
    if score_distance >= 35 and (n_factcheck >= 1 or n_academic >= 2
                                  or n_relevant >= 4):
        confidence = "HIGH"
    elif score_distance >= 20 and n_relevant >= 2:
        confidence = "MEDIUM"
    elif score_distance >= 10:
        confidence = "LOW"
    else:
        confidence = "LOW"

    # Force LOW if verdict was forced (score was 50 before override)
    if "[Low confidence]" in explanation:
        confidence = "LOW"

    # Calibrated label for UI
    if confidence == "HIGH" and score_distance >= 45:
        cal_conf = "Foarte sigur"          # score ≥ 95 or ≤ 5
    elif confidence == "HIGH" and score_distance >= 35:
        cal_conf = "Sigur"                 # score ≥ 85 or ≤ 15
    elif confidence == "MEDIUM" and n_factcheck >= 1:
        cal_conf = "Probabil corect -- confirmat de fact-checkers"
    elif confidence == "MEDIUM":
        cal_conf = "Probabil corect"
    elif "[Low confidence]" in explanation:
        cal_conf = "Nesigur -- dovezi insuficiente, verifică manual"
    else:
        cal_conf = "Nesigur -- verifică manual"

    result = VerifyResponse(
        claim=claim, score=score, verdict=verdict,
        confidence=confidence, explanation=explanation,
        topic=topic,
        supporting=supporting, contradicting=contradicting,
        neutral_sources=neutral,
        evidence_count=len(all_evidence),
        models_used=models_used,
        latency=latency,
        sub_claims=sub_claims if len(sub_claims) > 1 else [],
        word_importance=word_importance,
        calibrated_confidence=cal_conf,
    )
    # Always store in local diskcache as last resort
    cache.set(key, result.model_dump(), expire=3600 * 6)
    # Best-effort store in distributed semantic cache (Redis)
    try:
        from utils.semantic_cache import semantic_store
        await semantic_store(claim, result.model_dump())
    except Exception:
        pass
    return result


# ════════════════════════════════════════════════════════════
# EVIDENCE SOURCES
# ════════════════════════════════════════════════════════════

# Trusted domains for web search filtering
TRUSTED_DOMAINS = {
    # Encyclopedic (high priority for geography/history)
    "en.wikipedia.org", "wikipedia.org", "britannica.com",
    # Science & Academic
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nature.com",
    "science.org", "sciencedirect.com", "scholar.google.com",
    "who.int", "cdc.gov", "nih.gov", "nasa.gov", "epa.gov",
    # News (international, reliable)
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
    "theguardian.com", "nytimes.com", "washingtonpost.com",
    "economist.com", "ft.com", "digi24.ro", "g4media.ro",
    # Fact-checkers
    "factcheck.org", "snopes.com", "politifact.com",
    "fullfact.org", "verificat.cat", "correctiv.org",
    # Knowledge bases
    "britannica.com", "wolframalpha.com", "ourworldindata.org",
    "statista.com", "worldbank.org", "un.org", "europa.eu",
}