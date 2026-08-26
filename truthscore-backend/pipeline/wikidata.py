"""
TruthScore — Wikidata SPARQL verification.
"""
from config import *
from models import *

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
        tasks.append(search_counter_evidence(contradict_q))
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



    """
    Classify topic, translate, build factual search query.
    Returns (topic, search_query_en, claim_en)
    """
    if not gemini_client:
        return detect_topic(claim), _build_search_query(claim), claim

    safe = claim.replace('\\', '').replace('"', "'")[:300]

    prompt = f"""Analyze this claim and return ONE LINE of JSON.
Claim: "{safe}"

JSON only, no text before/after:
{{"t":"TOPIC","q":"max 5 english keywords","e":"english translation"}}

Topics: medical biology chemistry physics astronomy mathematics logic cs_tech engineering geography history literature art sports economics business climate politics sociology psychology philosophy ethics religion nutrition news general
- geography: mountains, rivers, countries, capitals, cities, space/astronomy facts about Earth
- science: physics, space, astronomy, chemistry
- sports: athletes, teams, games, records

Query rules: search for FACTS about the subject (not the claim). No superlatives.
Examples of correct JSON (english must be the FULL CLAIM translated, not the query!):
- Claim: "varful moldoveanu cel mai inalt din lume" -> {{"topic":"geography","query":"Moldoveanu Peak altitude height Romania","english":"Moldoveanu Peak is the highest peak in the world"}}
- Claim: "Great Wall visible from space" -> {{"topic":"geography","query":"Great Wall of China width visibility naked eye space","english":"The Great Wall of China is visible from space"}}
- Claim: "vaccinurile cauzeaza autism" -> {{"topic":"medical","query":"vaccines autism scientific studies","english":"Vaccines cause autism"}}
- Claim: "Romania joined EU in 2007" -> {{"topic":"geography","query":"Romania European Union accession 2007 date","english":"Romania joined the European Union in 2007"}}
- Claim: "Romania a aderat la UE in 2007" -> {{"topic":"geography","query":"Romania EU accession membership year","english":"Romania joined the European Union in 2007"}}

Return ONLY the JSON, nothing else. english = full claim in English."""

    try:
        import asyncio as _asyncio, json as _json
        loop = _asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                                config=make_gemini_config(max_tokens=300, use_search=False, thinking_budget=0)
            )
        )
        raw = resp.text.strip()
        # Strip markdown fences (Gemini sometimes wraps in ```json...```)
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        raw = re.sub(r"```\s*$", "", raw).strip()
        print(f"  [TOPIC-RAW] {raw[:200]!r}")

        # Initialize defaults
        topic, query, claim_en = "general", "", claim

        # Step 1: Try full JSON parse
        s, e2 = raw.find("{"), raw.rfind("}")
        if s != -1 and e2 > s:
            try:
                data = _json.loads(raw[s:e2+1])
                topic    = str(data.get("topic", data.get("t","general"))).lower().strip()
                query    = str(data.get("query",  data.get("q",""))).strip()
                claim_en = str(data.get("english", data.get("e", claim))).strip()
            except Exception:
                pass  # fall through to regex

        # Step 2: Always try regex as fallback/supplement
        # (works even on truncated JSON)
        # Support both long keys (topic/query/english) and short keys (t/q/e)
        tm = re.search(r'"(?:topic|t)"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
        qm = re.search(r'"(?:query|q)"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
        em = re.search(r'"(?:english|e)"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
        if tm and topic == "general": topic    = tm.group(1).lower().strip()
        if qm and not query:          query    = qm.group(1).strip()
        if em and claim_en == claim:  claim_en = em.group(1).strip()

        # Validate
        valid = set(DOMAIN_SOURCES.keys())
        if topic not in valid:
            topic = detect_topic(claim)
        if not query or len(query) < 3:
            query = _build_search_query(claim_en or claim)
        if not claim_en or len(claim_en) < 5:
            claim_en = claim

        print(f"  [TOPIC] {topic!r} | q: {query!r} | en: {claim_en[:60]!r}")
        return topic, query, claim_en

    except Exception as e:
        print(f"  [TOPIC] failed ({e}) -- fallback")
        return detect_topic(claim), _build_search_query(claim), claim
