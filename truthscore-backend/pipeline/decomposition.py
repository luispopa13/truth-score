"""
TruthScore -- Decomposition Pipeline
HyDE, FActScore, AVeriTeC, Wikidata SPARQL,
targeted queries, and search_with_queries.
"""
from config import *
from models import *
from pipeline.retrieval import *
from pipeline.ranking import rerank_with_crossencoder
from pipeline.helpers import (
    extract_keywords, detect_topic, is_nuance_claim, is_strict_domain,
    get_source_recency_weight,
)

def _get_llm():
    from pipeline.reasoning import call_llm_raw, reason_path_b
    return call_llm_raw, reason_path_b

async def call_llm_raw(prompt, max_tokens=1000, use_search=False, model="groq"):
    # Auxiliary pipeline calls (HyDE query generation, FActScore atom splitting,
    # AVeriTeC question decomposition) are simple structured tasks — route them
    # to cheap/fast Groq by default. _call_llm_raw_impl already falls back to
    # Gemini automatically if Groq errors, so quality is preserved on failure.
    _f, _ = _get_llm()
    return await _f(prompt, max_tokens=max_tokens, use_search=use_search, model=model)

async def reason_path_b(claim, top_evidence):
    _, _f = _get_llm()
    return await _f(claim, top_evidence)


async def hyde_generate_queries(claim: str) -> dict:
    """
    Step 1 of HyDE: generate hypothetical documents.
    Returns search queries derived from both confirming and denying docs.
    """
    lang = "ro" if any(c in RO_CHARS for c in claim) else "en"

    if lang == "ro":
        prompt = (
            f'Claim: "{claim}"\n\n'
            'Generează:\n'
            '1. Un paragraf scurt (2-3 propoziții) care CONFIRMĂ acest claim cu fapte specifice\n'
            '2. Un paragraf scurt (2-3 propoziții) care INFIRMĂ acest claim cu fapte specifice\n'
            '3. O interogare de căutare pentru a găsi dovezi academice/știri despre acest subiect\n\n'
            'Răspunde STRICT cu JSON:\n'
            '{"confirm_doc": "...", "deny_doc": "...", "neutral_query": "..."}'
        )
    else:
        prompt = (
            f'Claim: "{claim}"\n\n'
            'Generate:\n'
            '1. A short paragraph (2-3 sentences) that CONFIRMS this claim with specific facts\n'
            '2. A short paragraph (2-3 sentences) that DENIES/REFUTES this claim with specific facts\n'
            '3. A neutral search query to find academic/news evidence about this topic\n\n'
            'Respond STRICTLY with JSON:\n'
            '{"confirm_doc": "...", "deny_doc": "...", "neutral_query": "..."}'
        )

    try:
        import json as _json
        raw = await call_llm_raw(prompt, max_tokens=400, use_search=False)
        if not raw:
            return {}
        raw = raw.replace("```json","").replace("```","").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e > s:
            data = _json.loads(raw[s:e+1])
            print(f"  [HYDE] Generated confirm/deny documents")
            return data
    except Exception as ex:
        print(f"  [HYDE] Error: {ex}")
    return {}


async def hyde_retrieve(claim: str, hyde_data: dict) -> list[Source]:
    """
    Step 2 of HyDE: use hypothetical docs as search queries.
    Searches for real documents similar to BOTH confirming and denying docs.
    """
    sources = []

    # Extract key sentences from confirm/deny docs as search queries
    confirm_doc = hyde_data.get("confirm_doc", "")
    deny_doc    = hyde_data.get("deny_doc", "")
    neutral_q   = hyde_data.get("neutral_query", "")

    # Build targeted search queries from hypothetical documents
    queries = []
    if confirm_doc:
        # Take first sentence of confirm doc as supporting query
        first_sent = confirm_doc.split(".")[0].strip()[:120]
        queries.append(("support", first_sent))
    if deny_doc:
        # Take first sentence of deny doc as contradiction query
        first_sent = deny_doc.split(".")[0].strip()[:120]
        queries.append(("contradict", first_sent))
    if neutral_q:
        queries.append(("neutral", neutral_q[:120]))

    # Search for each query in parallel
    search_tasks = []
    for qtype, q in queries:
        if TAVILY_API_KEY:
            search_tasks.append((qtype, search_tavily(q)))
        search_tasks.append((qtype, search_ddg_wiki(q)))

    results = await asyncio.gather(
        *[task for _, task in search_tasks],
        return_exceptions=True
    )

    for (qtype, _), res in zip(search_tasks, results):
        if isinstance(res, list):
            for src in res:
                # Tag source with retrieval direction
                src.snippet = f"[{qtype.upper()}] {src.snippet}"
                sources.append(src)

    print(f"  [HYDE] Retrieved {len(sources)} sources via hypothetical docs")
    return sources[:12]




# ════════════════════════════════════════════════════════════
# LUNA 2 -- ATOMIC FACT DECOMPOSITION (FActScore approach)
# Paper: https://arxiv.org/abs/2305.14251
#
# Compound claims like "Moderate alcohol reduces heart disease"
# contain multiple sub-claims that need individual verification.
# FActScore decomposes → verifies each → aggregates.
#
# TRUE only if ALL critical sub-claims are supported.
# FALSE if ANY critical sub-claim is contradicted.
# UNCERTAIN if evidence is mixed across sub-claims.
# ════════════════════════════════════════════════════════════

async def decompose_into_atomic_facts(claim: str) -> list:
    """
    Decompose a compound claim into atomic, independently verifiable facts.
    Each atom is a single, simple statement that can be checked alone.
    Returns list of atomic fact strings, or [claim] if decomposition fails.
    """
    lang = "ro" if any(c in RO_CHARS for c in claim) else "en"

    if lang == "ro":
        prompt = (
            f'Afirmatie: "{claim}"\n\n'
            'Descompune in fapte atomice independente. Maxim 5 fapte.\n'
            'REGULA CRITICA: fiecare fapt contine EXACT o afirmatie verificabila — '
            'nu uni doi fapti intr-unul singur, nici daca amandoi sunt adevarati sau falsi. '
            'Daca o propozitie leaga doi fapti prin "si", "iar" sau "dar", desparte-i.\n'
            'Raspunde STRICT cu JSON: {"facts": ["fapt1", "fapt2"]}'
        )
    else:
        prompt = (
            f'Claim: "{claim}"\n\n'
            'Decompose this claim into atomic, independently verifiable facts.\n'
            'Rules:\n'
            '- CRITICAL: each fact contains EXACTLY ONE verifiable assertion; never merge two facts into one, even when both are true or both false.\n'
            '- If one sentence joins two facts with a connector (and, but, while), split them into separate facts.\n'
            '- Each fact must be a single, simple statement\n'
            '- No compound sentences\n'
            '- Include all implied sub-claims and hidden assumptions\n'
            '- Maximum 5 facts\n'
            '- If already atomic, return it as-is\n\n'
            'Respond STRICTLY with JSON:\n'
            '{"facts": ["fact1", "fact2", "fact3"]}'
        )

    try:
        import json as _json
        raw = await call_llm_raw(prompt, max_tokens=300, use_search=False)
        if not raw:
            return [claim]
        raw = raw.replace("```json","").replace("```","").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return [claim]
        data = _json.loads(raw[s:e+1])
        facts = [f for f in data.get("facts", []) if f and len(f) > 5]
        if not facts:
            return [claim]
        print(f"  [FACTSCORE] Decomposed into {len(facts)} atomic facts")
        for i, f in enumerate(facts, 1):
            print(f"    {i}. {f[:70]}")
        return facts[:5]
    except Exception as ex:
        print(f"  [FACTSCORE] Decomposition error: {ex}")
        return [claim]


async def verify_atomic_fact(fact: str, parent_evidence: list) -> dict:
    """
    Verify a single atomic fact using:
    1. Parent evidence (already retrieved for the compound claim)
    2. A targeted search specifically for this fact
    Returns verdict dict with score, verdict, explanation.
    """
    # Quick targeted search for this specific fact
    fact_queries = await generate_targeted_queries(fact)
    fact_evidence = await search_with_queries(fact_queries) if fact_queries else []  # noqa

    # Combine with parent evidence, dedup
    combined = parent_evidence[:6] + fact_evidence[:6]
    seen = set()
    deduped = []
    for src in combined:
        key = src.url.rstrip("/").lower().split("?")[0] or src.title
        if key not in seen:
            seen.add(key)
            deduped.append(src)

    # Use Path B stance classification on this specific fact
    if not deduped:
        return {"fact": fact, "verdict": "UNCERTAIN", "score": 50,
                "explanation": "No evidence found", "sources": []}

    b_score, b_verdict, b_conf, b_expl = await reason_path_b(fact, deduped[:8])

    if b_score is None:
        return {"fact": fact, "verdict": "UNCERTAIN", "score": 50,
                "explanation": "Could not classify evidence", "sources": deduped[:8]}

    return {
        "fact":        fact,
        "verdict":     b_verdict,
        "score":       b_score,
        "explanation": b_expl or "",
        "sources":     deduped[:8],
    }


async def factscore_verify(claim: str, top_evidence: list) -> tuple:
    """
    Full FActScore-style verification:
    1. Decompose claim into atomic facts
    2. Verify each fact independently
    3. Aggregate: TRUE only if all critical facts supported

    Returns (score, verdict, confidence, explanation, atomic_results)
    or (None, ...) if decomposition returns single fact (no benefit).
    """
    facts = await decompose_into_atomic_facts(claim)

    # If only one fact, no benefit from decomposition
    if len(facts) <= 1:
        return None, None, None, None, []

    # Verify all facts in parallel
    tasks = [verify_atomic_fact(f, top_evidence) for f in facts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    atom_results = []
    for r in results:
        if isinstance(r, dict):
            atom_results.append(r)
        elif isinstance(r, Exception):
            print(f"  [FACTSCORE] Atom error: {r}")
            atom_results.append({"fact": "", "verdict": "UNCERTAIN",
                                  "score": 50, "explanation": "Verification failed", "sources": []})

    if not atom_results:
        return None, None, None, None, []

    # Aggregate: weighted by how critical each fact is
    # Facts that are contradicted with HIGH score weigh more
    false_atoms = [a for a in atom_results if a["verdict"] == "FALSE"]
    true_atoms  = [a for a in atom_results if a["verdict"] == "TRUE"]
    unc_atoms   = [a for a in atom_results if a["verdict"] == "UNCERTAIN"]

    total = len(atom_results)
    print(f"  [FACTSCORE] TRUE={len(true_atoms)} FALSE={len(false_atoms)} "
          f"UNCERTAIN={len(unc_atoms)} / {total}")

    # Aggregation rules (FActScore-style):
    if false_atoms:
        # Any contradicted critical fact → FALSE
        worst = min(false_atoms, key=lambda a: a["score"])
        f_score  = worst["score"]
        f_verdict = "FALSE"
        f_expl = (f"Atomic decomposition found {len(false_atoms)}/{total} "
                  f"sub-claims to be FALSE. Key contradiction: {worst['explanation'][:150]}")
    elif len(true_atoms) == total:
        # All facts supported → TRUE
        avg = sum(a["score"] for a in true_atoms) // len(true_atoms)
        f_score   = avg
        f_verdict = "TRUE"
        f_expl = (f"All {total} atomic sub-claims are supported. "
                  f"Evidence consistently confirms the claim.")
    else:
        # Mixed results → UNCERTAIN
        avg = sum(a["score"] for a in atom_results) // total
        f_score   = max(VERDICT_FALSE_AT, min(VERDICT_TRUE_AT - 1, avg))  # force into UNCERTAIN zone
        f_verdict = "UNCERTAIN"
        f_expl = (f"Mixed evidence: {len(true_atoms)} sub-claims supported, "
                  f"{len(false_atoms)} contradicted, {len(unc_atoms)} unclear.")

    confidence = "HIGH" if abs(f_score - 50) >= 30 else "MEDIUM"

    return f_score, f_verdict, confidence, f_expl, atom_results


# ════════════════════════════════════════════════════════════
# LUNA 3 -- AVeriTeC QUESTION DECOMPOSITION
# Paper: https://arxiv.org/abs/2305.13117
#
# Instead of searching for the claim directly, generate
# verification questions and answer each with retrieval.
# Especially effective for political and ambiguous claims.
# ════════════════════════════════════════════════════════════

async def averitec_generate_questions(claim: str) -> list:
    """
    Generate verification questions for a claim (AVeriTeC approach).
    Questions designed to elicit evidence that confirms OR refutes.
    """
    lang = "ro" if any(c in RO_CHARS for c in claim) else "en"

    if lang == "ro":
        prompt = (
            f'Afirmatie: "{claim}"\n\n'
            'Genereaza 3-4 intrebari pentru verificare. '
            'Incluzi intrebari care pot CONFIRMA dar si INFIRMA.\n'
            'Raspunde STRICT cu JSON: {"questions": ["Q1?", "Q2?", "Q3?"]}'
        )
    else:
        prompt = (
            f'Claim: "{claim}"\n\n'
            'Generate 3-4 specific questions to verify this claim.\n'
            'Include questions that could both CONFIRM and REFUTE it.\n'
            'Make them concrete and answerable with web search.\n\n'
            'Respond STRICTLY with JSON:\n'
            '{"questions": ["Question 1?", "Question 2?", "Question 3?"]}'
        )

    try:
        import json as _json
        raw = await call_llm_raw(prompt, max_tokens=250, use_search=False)
        if not raw:
            return []
        raw = raw.replace("```json","").replace("```","").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return []
        data      = _json.loads(raw[s:e+1])
        questions = [q for q in data.get("questions", []) if q and "?" in q]
        print(f"  [AVERITEC] Generated {len(questions)} verification questions")
        for i, q in enumerate(questions, 1):
            print(f"    {i}. {q[:70]}")
        return questions[:4]
    except Exception as ex:
        print(f"  [AVERITEC] Question generation error: {ex}")
        return []


async def averitec_answer_question(question: str) -> dict:
    """Answer a single verification question using retrieval + Gemini."""
    # These three searches are independent — fire them concurrently instead of
    # awaiting one at a time (~3x faster per question). Order of results is
    # preserved (tavily, ddg_wiki, semantic_scholar) for deterministic ranking.
    search_tasks = []
    if TAVILY_API_KEY:
        search_tasks.append(search_tavily(question))
    search_tasks.append(search_ddg_wiki(question))
    search_tasks.append(search_semantic_scholar(question))
    results = await asyncio.gather(*search_tasks, return_exceptions=True)
    sources = []
    for r in results:
        if isinstance(r, list):
            sources += r

    if not sources:
        return {"question": question, "answer": "No evidence found", "stance": "NEUTRAL"}

    ev_text = "\n".join(
        f"[{i+1}] {s.publisher}: {s.snippet[:200]}"
        for i, s in enumerate(sources[:5])
    )

    prompt = (
        f'Question: "{question}"\n\n'
        f'Evidence:\n{ev_text}\n\n'
        'Answer in 1-2 sentences. State if answer SUPPORTS, CONTRADICTS, '
        'or is NEUTRAL to a claim about this topic.\n'
        'JSON: {"answer": "...", "stance": "SUPPORTS|CONTRADICTS|NEUTRAL"}'
    )

    try:
        import json as _json
        raw = await call_llm_raw(prompt, max_tokens=200, use_search=False)
        if not raw:
            return {"question": question, "answer": "No answer", "stance": "NEUTRAL"}
        raw = raw.replace("```json","").replace("```","").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return {"question": question, "answer": raw[:100], "stance": "NEUTRAL"}
        data = _json.loads(raw[s:e+1])
        return {
            "question": question,
            "answer":   data.get("answer", ""),
            "stance":   data.get("stance", "NEUTRAL").upper(),
            "sources":  sources[:3],
        }
    except Exception as ex:
        print(f"  [AVERITEC] Answer error: {ex}")
        return {"question": question, "answer": "Error", "stance": "NEUTRAL"}


async def averitec_verify(claim: str) -> tuple:
    """
    Full AVeriTeC verification:
    1. Generate verification questions
    2. Answer each question with retrieval
    3. Aggregate answers into final verdict

    Returns (score, verdict, confidence, explanation)
    or (None, ...) if questions could not be generated.
    """
    questions = await averitec_generate_questions(claim)
    if not questions:
        return None, None, None, None

    # Answer all questions in parallel
    tasks = [averitec_answer_question(q) for q in questions]
    qa_results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_qa = [r for r in qa_results if isinstance(r, dict)]
    if not valid_qa:
        return None, None, None, None

    # Count stances
    supports    = [r for r in valid_qa if r.get("stance") == "SUPPORTS"]
    contradicts = [r for r in valid_qa if r.get("stance") == "CONTRADICTS"]
    neutrals    = [r for r in valid_qa if r.get("stance") == "NEUTRAL"]

    total = len(valid_qa)
    print(f"  [AVERITEC] Q&A: supports={len(supports)} "
          f"contradicts={len(contradicts)} neutral={len(neutrals)}")

    # Aggregate
    if len(contradicts) > len(supports):
        score    = 15 + (len(contradicts) * 5)
        verdict  = "FALSE"
        conf     = "HIGH" if len(contradicts) >= 2 else "MEDIUM"
        expl     = (f"Verification questions reveal contradictions: "
                   + "; ".join(f'"{r["question"][:50]}": {r["answer"][:80]}'
                                for r in contradicts[:2]))
    elif len(supports) > 0 and len(contradicts) == 0:
        score    = 75 + (len(supports) * 5)
        verdict  = "TRUE"
        conf     = "HIGH" if len(supports) >= 2 else "MEDIUM"
        expl     = (f"All verification questions answered positively: "
                   + "; ".join(f'"{r["question"][:50]}": {r["answer"][:80]}'
                                for r in supports[:2]))
    else:
        score    = 50
        verdict  = "UNCERTAIN"
        conf     = "LOW"
        expl     = (f"Mixed answers to verification questions: "
                   + "; ".join(f'"{r["question"][:40]}": {r["answer"][:60]}'
                                for r in valid_qa[:2]))

    return min(100, max(0, score)), verdict, conf, expl


# ════════════════════════════════════════════════════════════
# LUNA 4 -- WIKIDATA SPARQL (Structured fact verification)
# For factual claims about geography, people, dates, counts.
# These are the easiest to get wrong with fuzzy text search.
# ════════════════════════════════════════════════════════════

async def wikidata_sparql_verify(claim: str) -> list:
    """
    Generate and execute a SPARQL query on Wikidata to verify
    structured facts (capitals, populations, birth dates, counts, etc.)
    No API key required. Uses Wikidata public endpoint.
    """
    check_prompt = (
        f'Claim: "{claim}"\n\n'
        'Can this claim be verified with a simple Wikidata SPARQL query? '
        'Eligible: capitals, populations, birth dates, country membership, '
        'geographic records (highest mountain, longest river, etc.).\n\n'
        'If YES, write the SPARQL query only (SELECT WHERE format).\n'
        'Use standard Wikidata prefixes (wd:, wdt:, rdfs:).\n'
        'If NO, respond: NO'
    )

    try:
        sparql = await call_llm_raw(check_prompt, max_tokens=300, use_search=False)
        if not sparql or sparql.strip().upper() == "NO":
            return []

        sparql = sparql.strip()
        # Safety: no write operations
        if any(w in sparql.upper() for w in ["DELETE", "INSERT", "DROP", "UPDATE"]):
            return []

        print(f"  [WIKIDATA] Executing SPARQL")

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://query.wikidata.org/sparql",
                params={"query": sparql, "format": "json"},
                headers={"User-Agent": "TruthScore/11.0 (fact-checking research)"},
            )
            if r.status_code != 200:
                print(f"  [WIKIDATA] HTTP {r.status_code}")
                return []

            data     = r.json()
            bindings = data.get("results", {}).get("bindings", [])
            if not bindings:
                print(f"  [WIKIDATA] No results")
                return []

            result_text = "Wikidata structured data: "
            for b in bindings[:3]:
                vals = [f"{k}={v.get('value','')}" for k, v in b.items()]
                result_text += "; ".join(vals) + ". "

            print(f"  [WIKIDATA] {len(bindings)} result(s)")
            return [Source(
                type      = "academic",
                title     = f"Wikidata SPARQL: {claim[:60]}",
                url       = "https://query.wikidata.org",
                snippet   = result_text[:400],
                publisher = "Wikidata",
            )]

    except Exception as ex:
        print(f"  [WIKIDATA] Error: {ex}")
        return []




async def generate_targeted_queries(claim: str) -> dict:
    """
    Generate 3 targeted search queries for balanced evidence retrieval.

    Instead of searching once with the raw claim, we generate:
    - A query to find SUPPORTING evidence
    - A query to find CONTRADICTING / debunking evidence
    - A query to find SCIENTIFIC CONSENSUS or expert opinion

    This guarantees retrieval finds evidence from both directions,
    which is the core fix for claims where Gemini's training data
    is biased or outdated (e.g. medical consensus that changed).

    No hardcoding — Gemini generates the queries dynamically for
    any claim in any domain or language.
    """
    lang = "ro" if any(c in RO_CHARS for c in claim) else "en"

    if lang == "ro":
        prompt = (
            f'Afirmatie: "{claim}"\n\n'
            'Genereaza 3 interogari de cautare pentru a verifica aceasta afirmatie:\n'
            '1. O interogare pentru a gasi dovezi care CONFIRMA afirmatia\n'
            '2. O interogare pentru a gasi dovezi care INFIRMA sau dezmint afirmatia\n'
            '3. O interogare pentru a gasi consensul stiintific sau opinia expertilor\n\n'
            'Raspunde STRICT cu JSON:\n'
            '{"support": "...", "contradict": "...", "consensus": "..."}'
        )
    else:
        prompt = (
            f'Claim: "{claim}"\n\n'
            'Generate 3 targeted search queries to fact-check this claim:\n'
            '1. A query to find evidence SUPPORTING this claim\n'
            '2. A query to find evidence CONTRADICTING or DEBUNKING this claim\n'
            '   (include words like: myth, false, debunked, no evidence, overturned)\n'
            '3. A query to find SCIENTIFIC CONSENSUS or expert opinion on this topic\n\n'
            'Make each query specific and distinct. Do not repeat the claim verbatim.\n'
            'Respond STRICTLY with JSON:\n'
            '{"support": "...", "contradict": "...", "consensus": "..."}'
        )

    try:
        import json as _json
        raw = await call_llm_raw(prompt, max_tokens=200, use_search=False)
        if not raw:
            return {}
        raw = raw.replace("```json", "").replace("```", "").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e > s:
            data = _json.loads(raw[s:e+1])
            queries = {k: v for k, v in data.items()
                       if k in ("support", "contradict", "consensus") and v}
            print(f"  [QUERIES] Generated {len(queries)} targeted queries")
            for k, v in queries.items():
                print(f"    {k}: {v[:60]}")
            return queries
    except Exception as ex:
        print(f"  [QUERIES] Error: {ex}")
    return {}


async def search_with_queries(queries: dict) -> list:
    """
    Run targeted searches using the generated queries.
    Returns combined evidence from all 3 query directions.
    Tag each source with its retrieval direction for Path B scoring.
    """
    tasks   = []
    qlabels = []

    support_q   = queries.get("support", "")
    contradict_q = queries.get("contradict", "")
    consensus_q  = queries.get("consensus", "")

    # Search each query with best available source
    if support_q:
        if TAVILY_API_KEY:
            tasks.append(search_tavily(support_q))
            qlabels.append("support")
        tasks.append(search_ddg_wiki(support_q))
        qlabels.append("support")

    if contradict_q:
        if TAVILY_API_KEY:
            tasks.append(search_tavily(contradict_q))
            qlabels.append("contradict")
        from pipeline.source_plan import search_counter_evidence as _sce
        tasks.append(_sce(contradict_q))
        qlabels.append("contradict")

    if consensus_q:
        tasks.append(search_semantic_scholar(consensus_q))
        qlabels.append("consensus")
        if TAVILY_API_KEY:
            tasks.append(search_tavily(consensus_q))
            qlabels.append("consensus")

    if not tasks:
        return []

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_sources = []
    for label, res in zip(qlabels, results):
        if isinstance(res, list):
            for src in res:
                # Tag snippet with retrieval direction
                # This helps Path B understand the provenance
                if label == "contradict" and not src.snippet.startswith("[CONTRADICT]"):
                    src.snippet = f"[CONTRADICT] {src.snippet}"
                elif label == "support" and not src.snippet.startswith("[SUPPORT]"):
                    src.snippet = f"[SUPPORT] {src.snippet}"
                all_sources.append(src)

    print(f"  [QUERIES] Retrieved {len(all_sources)} sources from targeted queries")
    return all_sources
