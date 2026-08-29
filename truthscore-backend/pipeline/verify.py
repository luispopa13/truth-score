"""
TruthScore — Main verification pipeline.
"""
from dataclasses import dataclass
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
from pipeline.aggregate import build_sub_claim_results, aggregate_score
from pipeline.helpers import (
    normalize_claim, is_temporal_claim, is_nuance_claim, is_strict_domain,
    get_source_recency_weight, build_nuance_queries,
    split_claims, compute_word_importance, smart_detect_topic,
    detect_topic, evaluate_math_claim, extract_keywords,
    _domain, factcheck_rating_to_nli,
)

@dataclass
class RetrievalResult:
    """Everything /verify, batch, and PDF need out of the shared retrieval+rank stage."""
    topic:        str
    search_query: str
    claim_en:     str
    top_k:        list
    rest:         list
    all_evidence: list
    models_used:  list
    t_retrieval:  float
    t_embedding:  float


async def retrieve_and_rank(claim: str, *, eco: bool = False) -> RetrievalResult:
    """Shared evidence retrieval + two-stage ranking for /verify, batch, and PDF.

    Runs the full domain-routed retrieval cascade (topic detection, HyDE,
    targeted queries, counter-evidence, per-source time budgets, URL dedup, and
    the fallback_only paid-Tavily top-up) followed by embedding + cross-encoder
    reranking. Returns the ranked top_k / rest, the full deduped evidence, the
    detected topic/search_query/claim_en, model tags accrued so far, and the
    retrieval/embedding timings. Single source of truth so batch and PDF stop
    reimplementing an inferior flat-5s copy of the /verify pipeline.
    """
    import time as _t
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
    if eco:
        # Eco mode (heavy-day paid user past threshold): free sources only,
        # no paid Tavily spend — margin protection without a hard block.
        TAVILY_MODE = "off"
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
        # Label must contain a _SLOW_KEYS token (this IS a ddg/wiki search) so it
        # gets the 12s slow budget, not the 7s fast one — otherwise it times out
        # before returning, killing the counter-evidence nuance search entirely.
        labels.append("NUANCE_DDG_WIKI")
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
    # Always add OpenAlex — 250M works, keyless. Broadest free academic index;
    # self-guards (returns [] when the claim isn't scholarly) so it's a cheap
    # backstop that never needs an API key.
    if "OPENALEX" not in labels:
        evidence_tasks.append(search_openalex(search_query))
        labels.append("OPENALEX_FALLBACK")
    # Per-source time budgets. Fast structured APIs are capped tight, but the
    # web / search fallbacks (DDG-wiki, Britannica scrape, counter-evidence,
    # Tavily, GDELT) legitimately take longer — their own internal timeouts are
    # 10-15s. The old flat 5s cap killed them *before* they could ever return,
    # so the primary web fallbacks contributed zero evidence. Sources run
    # concurrently in the gather below, so the total retrieval time is bounded
    # by the slowest budget, not their sum. (Priorities: evidence quality > latency.)
    _SLOW_SOURCE_BUDGET = float(os.getenv("SLOW_SOURCE_TIMEOUT", "12"))
    _FAST_SOURCE_BUDGET = float(os.getenv("FAST_SOURCE_TIMEOUT", "7"))
    _SLOW_KEYS = ("DDG", "WIKI", "BRITANNICA", "COUNTER", "TAVILY", "GDELT", "SCRAPE")

    def _budget_for(label: str) -> float:
        L = (label or "").upper()
        return _SLOW_SOURCE_BUDGET if any(k in L for k in _SLOW_KEYS) else _FAST_SOURCE_BUDGET

    async def _with_timeout(coro, label):
        budget = _budget_for(label)
        try:
            return await asyncio.wait_for(coro, timeout=budget)
        except asyncio.TimeoutError:
            print(f"  [{label}] TIMEOUT (>{budget:.0f}s)")
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
    tavily_topup_used = False
    if (TAVILY_MODE == "fallback_only" and TAVILY_API_KEY
            and "TAVILY_FALLBACK" not in labels
            and len(all_evidence) < MIN_EVIDENCE):
        print(f"  [TAVILY] Free evidence thin ({len(all_evidence)}) -> paid top-up")
        try:
            tavily_results = await asyncio.wait_for(
                search_tavily(search_query), timeout=12.0)
            print(f"  [TAVILY-TOPUP] +{len(tavily_results)} sources")
            all_evidence.extend(tavily_results)
            tavily_topup_used = True
        except Exception as e:
            print(f"  [TAVILY-TOPUP] failed: {e}")

    print(f"  TOTAL: {len(all_evidence)} evidence | retrieval: {t_retrieval:.0f}ms")

    # ── Step 2: Two-stage relevance ranking ──────────────────────
    # Stage A: Embedding cosine similarity (fast, high recall) -> top 30
    # Stage B: Cross-encoder reranking (slower, high precision) -> top 12
    models_used = []
    if tavily_topup_used:
        models_used.append("tavily-topup")
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

    return RetrievalResult(
        topic=topic, search_query=search_query, claim_en=claim_en,
        top_k=top_k, rest=rest, all_evidence=all_evidence,
        models_used=models_used, t_retrieval=t_retrieval, t_embedding=t_embedding,
    )


async def verify_claim(req: VerifyRequest, eco: bool = False):
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

    # ── Thundering-herd guard ─────────────────────────────────
    # SETNX claims this computation. If N concurrent requests arrive for the
    # same claim before the first finishes, only the first computes; the rest
    # poll the semantic cache and return the result when it appears — paying
    # zero extra LLM cost. The 30s TTL is the safety-net for compute failures.
    _inflight_key = f"ts:inflight:{key}"
    _got_lock = True    # default: no Redis, every worker computes freely
    _lock_redis = None
    try:
        from utils.redis_client import get_async_redis as _get_redis
        _lock_redis = _get_redis()
        if _lock_redis:
            _got_lock = bool(await _lock_redis.set(_inflight_key, "1", nx=True, ex=30))
            if not _got_lock:
                # Another worker is computing — poll semantic cache up to 25 s
                for _ in range(50):
                    await asyncio.sleep(0.5)
                    try:
                        from utils.semantic_cache import semantic_lookup as _sl
                        _hit = await _sl(claim)
                        if _hit:
                            _hit["cached"] = True
                            return VerifyResponse(**_hit)
                    except Exception:
                        pass
                # Timed out (compute node may have died) — fall through and compute
                _got_lock = True
    except Exception:
        _got_lock = True  # Redis unavailable — compute normally

    # Everything past the herd-lock acquisition runs under a try/finally so the
    # inflight lock is ALWAYS released — on the math-shortcut early return, on a
    # normal return, and on an exception mid-pipeline. Leaking it used to pin the
    # key for its full 30 s TTL, so a single failed compute stalled every retry
    # and every concurrent waiter for ~25 s.
    try:
        return await _verify_compute(claim, key, eco, t_total_start)
    except Exception as _e:
        print(f"  [VERIFY] compute failed for '{claim[:60]}': {type(_e).__name__}: {_e}")
        # Degrade to an honest UNCERTAIN rather than surfacing a 500 — keeps the
        # app responsive when a downstream provider (LLM, search, NLI) hiccups.
        return VerifyResponse(
            claim=claim, score=50, verdict="UNCERTAIN", confidence="LOW",
            explanation="Verification could not be completed due to a temporary "
                        "error. Please try again in a moment.",
            models_used=["error-fallback"],
        )
    finally:
        try:
            if _got_lock and _lock_redis:
                await _lock_redis.delete(_inflight_key)
        except Exception:
            pass


async def _verify_compute(claim: str, key: str, eco: bool, t_total_start: float):
    """Core verification compute: retrieval → reasoning → aggregate → response.

    Split out of verify_claim so the thundering-herd lock release can wrap the
    whole thing in one try/finally. Assumes the semantic-cache miss and the
    inflight-lock acquisition already happened in verify_claim.
    """
    import time as _t

    # ── Math shortcut ─────────────────────────────────────────
    math_result = evaluate_math_claim(claim)
    if math_result:
        score, expl = math_result
        verdict    = "TRUE" if score >= VERDICT_TRUE_AT else ("FALSE" if score < VERDICT_FALSE_AT else "UNCERTAIN")
        result = VerifyResponse(
            claim=claim, score=score, verdict=verdict,
            confidence="HIGH", explanation=expl,
            models_used=["mathematical-evaluator"],
        )
        cache.set(key, result.model_dump(), expire=3600 * 24)
        return result

    # ── Steps 1+2: shared evidence retrieval + two-stage ranking ──
    # Extracted into retrieve_and_rank() so /verify, batch, and PDF share ONE
    # retrieval+rank implementation (per-source budgets, dedup, cross-encoder).
    _rr = await retrieve_and_rank(claim, eco=eco)
    topic, search_query, claim_en = _rr.topic, _rr.search_query, _rr.claim_en
    top_k, rest, all_evidence = _rr.top_k, _rr.rest, _rr.all_evidence
    models_used = _rr.models_used
    t_retrieval, t_embedding = _rr.t_retrieval, _rr.t_embedding

    # ── Step 3: Path A — Gemini reasoning ────────────────────
    t3 = _t.time()
    # Per-sub-claim breakdown for compound claims (filled by FActScore below).
    sub_results = []
    # Split early (cheap) so we can force atomic decomposition on genuinely
    # compound claims even when Path A is HIGH-confidence — a compound claim
    # needs its parts scored individually, not just an overall verdict.
    sub_claims = await split_claims(claim)
    is_compound = len(sub_claims) > 1
    if gemini_client and top_k:
        # Wikidata SPARQL is independent of the LLM verdict chain (it only adds
        # structured sources for geo/history/politics/science). Launch it now so
        # it runs concurrently with Path A/B/FActScore/AVeriTeC instead of
        # tacking a serial SPARQL round-trip onto the end.
        wikidata_task = None
        if not eco and topic in ("geography", "history", "politics", "science"):
            wikidata_task = asyncio.create_task(wikidata_sparql_verify(claim))
        try:
            # ── MODEL ROUTING with quality-escalation ──────────────
            # 1. pick_model(): easy short claims -> cheap Groq GPT-OSS;
            #    hard signals -> straight to Gemini.
            # 2. If the cheap result comes back weak (LOW/UNCERTAIN), re-run
            #    ONCE with Gemini — users never see degraded verdicts; extra
            #    latency lands only on genuinely hard claims.
            claim_for_llm = claim_en if claim_en != claim else claim
            chosen_model = pick_model(claim, topic, eco)

            score, verdict, confidence, explanation, supporting, contradicting, neutral = \
                await reason_with_gpt(claim_for_llm, top_k, rest, model_hint=chosen_model)
            models_used.append(chosen_model)

            if not eco and chosen_model not in ("gemini",) and (verdict == "UNCERTAIN" or confidence == "LOW"):
                print(f"  [ROUTE] cheap result weak ({verdict}/{confidence}) -> escalating to Gemini")
                score, verdict, confidence, explanation, supporting, contradicting, neutral = \
                    await reason_with_gpt(claim_for_llm, top_k, rest, model_hint="gemini")
                models_used.append("gemini-escalation")

            scored = top_k + rest

            # ── Path B — Evidence-based cross-check ──────────────
            # Runs only when Path A result is ambiguous or claim is high-risk.
            # Path B classifies stance of each source mathematically —
            # no LLM parametric memory involved in the final verdict.
            if not eco and _path_b_triggers(score, verdict, claim, topic):
                print(f"  [PATH-B] Triggered (score={score}, nuance={is_nuance_claim(claim)})")
                b_score, b_verdict, b_conf, b_expl = await reason_path_b(claim, top_k)

                if b_score is not None and b_verdict is not None:
                    a_distance       = abs(score - 50)
                    b_distance       = abs(b_score - 50)
                    nuance_or_strict = is_nuance_claim(claim) or is_strict_domain(claim, topic)
                    # Snapshot BEFORE any reassignment below — the explanation
                    # strings reference the *initial* (Path A) verdict, but the
                    # reassignments overwrite `verdict` first, so reading it later
                    # would echo the new verdict as if it were the original.
                    orig_verdict = verdict

                    if verdict != b_verdict:
                        if nuance_or_strict or b_distance > a_distance + 10:
                            print(f"  [PATH-B] Override: A={verdict}({score}) "
                                  f"-> B={b_verdict}({b_score}) "
                                  f"({'evidence-priority' if nuance_or_strict else 'more decisive'})")
                            score       = b_score
                            verdict     = b_verdict
                            confidence  = b_conf
                            explanation = (f"[Evidence-based] {b_expl} "
                                          f"(Initial assessment was {orig_verdict})")
                            models_used.append("evidence-stance-classifier")
                        else:
                            print(f"  [PATH-B] Conflict -> UNCERTAIN")
                            score       = 50
                            verdict     = "UNCERTAIN"
                            confidence  = "LOW"
                            explanation = (f"Conflicting evidence: sources suggest "
                                          f"{b_verdict} but overall assessment "
                                          f"suggested {orig_verdict}. {b_expl}")
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
            # In eco mode we still decompose genuinely compound claims (the
            # per-sub-claim breakdown is a core product promise), but skip the
            # extra accuracy-boosting FActScore runs on single doubtful claims.
            if (is_compound or (not eco and (confidence != "HIGH"
                    or is_nuance_claim(claim) or is_strict_domain(claim, topic)))):
                print(f"  [FACTSCORE] Running (score={score}, compound={is_compound})")
                fs_score, fs_verdict, fs_conf, fs_expl, atom_results = \
                    await factscore_verify(claim, top_k)
                if atom_results:
                    # Reclaim the per-atom breakdown + sources the pipeline already
                    # computed. This is the source->sub-claim mapping the product needs.
                    sub_results = build_sub_claim_results(atom_results)
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
            if not eco and verdict == "UNCERTAIN":
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
            # Task was launched at the top of this block; collect it here and
            # surface the structured sources into the response so the user
            # actually sees them (they're evidence, not just an internal signal).
            if wikidata_task is not None:
                wikidata_srcs = await wikidata_task
                wikidata_task = None  # collected — nothing left to clean up
                if wikidata_srcs:
                    for s in wikidata_srcs:
                        s.stance = "neutral"
                    neutral = wikidata_srcs + neutral
                    print(f"  [WIKIDATA] Surfaced {len(wikidata_srcs)} structured sources")
        finally:
            # If reasoning raised before we collected it, don't leak a pending
            # SPARQL task (silent "Task was destroyed but it is pending" + a
            # dangling network round-trip).
            if wikidata_task is not None and not wikidata_task.done():
                wikidata_task.cancel()
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

    # ── Weighted aggregate over sub-claims ────────────────────
    # When the claim decomposed into multiple sub-claims, the overall verdict is
    # the authority-weighted aggregate of the parts (not a plain mean), with a
    # hard gate forcing FALSE on a decisively-false authoritative sub-claim.
    # This MUST run BEFORE the word-importance + calibrated-confidence below,
    # because it overrides score/verdict/confidence. Computing those
    # explainability fields from the pre-aggregate verdict made the UI's
    # confidence label and highlighted words contradict the final score bar.
    aggregate_reason = ""
    if sub_results:
        agg_score, agg_verdict, agg_conf, aggregate_reason = aggregate_score(sub_results)
        score, verdict, confidence = agg_score, agg_verdict, agg_conf
        # The override just changed the verdict, so the Path-A/B `explanation` and
        # the whole-claim supporting/contradicting/neutral lists produced earlier
        # can now CONTRADICT the aggregate verdict on the score bar (e.g. the text
        # still argues TRUE while the aggregate says UNCERTAIN). Rebuild both from
        # the sub-claim results — the evidence the aggregate is actually computed
        # from — so the narrative, the source columns, and the score all agree.
        explanation = aggregate_reason or explanation
        rebuilt_sup, rebuilt_con, rebuilt_neu = [], [], []
        for sc in sub_results:
            rebuilt_sup.extend(sc.supporting)
            rebuilt_con.extend(sc.contradicting)
            rebuilt_neu.extend(sc.neutral_sources)
        # Fold back any WHOLE-CLAIM source not mapped to a sub-claim
        # (claim_index == -1) — most importantly the Wikidata structured sources
        # stamped neutral above — so surfacing the aggregate never drops evidence.
        seen_urls = {(s.url or "").rstrip("/").lower()
                     for s in rebuilt_sup + rebuilt_con + rebuilt_neu}
        for s in supporting + contradicting + neutral:
            if getattr(s, "claim_index", -1) != -1:
                continue  # already represented through its sub-claim
            u = (s.url or "").rstrip("/").lower()
            if u and u in seen_urls:
                continue
            if u:
                seen_urls.add(u)
            st = (s.stance or "").lower()
            if st == "supporting":
                rebuilt_sup.append(s)
            elif st == "contradicting":
                rebuilt_con.append(s)
            else:
                rebuilt_neu.append(s)
        supporting, contradicting, neutral = rebuilt_sup, rebuilt_con, rebuilt_neu

    # Claim splitting & explainability (runs fast, parallel with cache write)
    # sub_claims already computed early (before reasoning) to gate decomposition.
    word_importance = compute_word_importance(claim, verdict, score)

    # ── Real confidence calibration ───────────────────────────
    # Based on: score distance from 50, source quality, agreement. For a
    # decomposed claim this refines the aggregate's confidence from the FINAL
    # aggregate score + whole-claim source mix, so it stays consistent with the
    # score bar the user sees.
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

    # Calibrated label for UI — emit a LANGUAGE-NEUTRAL enum key, not a
    # hardcoded string. The client (Dashboard I18N / extension i18n.js) maps the
    # key to the active UI language via t(); previously this shipped Romanian
    # text that never translated for the EN-default audience.
    if confidence == "HIGH" and score_distance >= 45:
        cal_conf = "calVeryHigh"           # score ≥ 95 or ≤ 5
    elif confidence == "HIGH" and score_distance >= 35:
        cal_conf = "calHigh"               # score ≥ 85 or ≤ 15
    elif confidence == "MEDIUM" and n_factcheck >= 1:
        cal_conf = "calLikelyFactcheck"
    elif confidence == "MEDIUM":
        cal_conf = "calLikely"
    elif "[Low confidence]" in explanation:
        cal_conf = "calLowInsufficient"
    else:
        cal_conf = "calLow"

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
        sub_claim_results=sub_results,
        aggregate_reason=aggregate_reason,
    )
    # Always store in local diskcache as last resort
    cache.set(key, result.model_dump(), expire=3600 * 6)
    # Best-effort store in distributed semantic cache (Redis)
    try:
        from utils.semantic_cache import semantic_store
        await semantic_store(claim, result.model_dump())
    except Exception:
        pass
    # NB: the thundering-herd inflight lock is released by verify_claim's
    # finally block (which wraps this whole function), so waiting requests pick
    # up the freshly-cached result on their next poll.
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