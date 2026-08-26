"""
TruthScore -- Domain Routing and Source Plan
DOMAIN_SOURCES mapping, build_source_plan, counter evidence search.
"""
from config import *
from models import *
from pipeline.retrieval import *
from pipeline.helpers import (
    is_temporal_claim, is_nuance_claim, is_strict_domain,
    build_nuance_queries, extract_keywords, detect_topic,
)


DOMAIN_SOURCES = {
    # Medicine: peer-reviewed + FDA + fact-check
    "medical":     ["tavily", "pubmed", "europe_pmc", "openfda",
                    "who", "cdc", "semantic_scholar", "crossref", "factcheck"],
    # Biology: life sciences
    "biology":     ["pubmed", "europe_pmc", "ncbi",
                    "semantic_scholar", "crossref", "factcheck"],
    # Chemistry: chemical databases
    "chemistry":   ["pubchem", "semantic_scholar", "crossref",
                    "europe_pmc", "arxiv"],
    # Physics / Astronomy / Space
    "physics":     ["tavily", "britannica", "arxiv", "semantic_scholar", "nasa",
                    "crossref", "factcheck"],
    # Mathematics
    "mathematics": ["wolfram", "arxiv", "openalex_math", "crossref", "semantic_scholar"],
    # CS / AI / Software
    "cs_tech":     ["tavily", "semantic_scholar", "arxiv", "crossref", "factcheck"],
    # Engineering
    "engineering": ["semantic_scholar", "arxiv", "crossref"],
    # Geography: structured geo data + encyclopedic + OSM
    "geography":   ["tavily", "britannica", "wikidata_geo", "geonames", "rest_countries",
                    "nominatim"],
    # History: encyclopedic + academic + UNESCO + NPS
    "history":     ["tavily", "britannica", "wikidata_geo", "loc", "crossref", "semantic_scholar"],
    # Literature: book databases + academic
    "literature":  ["open_library", "crossref", "semantic_scholar",
                    "wikidata_geo", "doaj"],
    # Art / Architecture / Music / Film
    "art":         ["europeana", "met_museum", "smithsonian",
                    "wikidata_geo", "crossref"],
    # Sports: tier-1 news + sport-specific
    "sports":      ["sportsdb", "football_data", "nba_stats", "f1",
                    "guardian", "newsapi", "news_rss"],
    # Economics / Finance
    "economics":   ["world_bank", "imf", "oecd",
                    "crossref", "newsapi"],
    # Climate / Environment
    "climate":     ["noaa", "nasa", "arxiv", "epa",
                    "europe_pmc", "factcheck", "guardian"],
    # Politics / Law
    "politics":    ["eu_data", "wikidata_geo", "govtrack",
                    "newsapi", "guardian", "gdelt", "factcheck"],
    # Current News
    "news":        ["tavily", "newsapi", "guardian", "gdelt",
                    "news_rss", "factcheck"],
    # Logic / Formal logic
    "logic":       ["sep", "arxiv", "crossref", "semantic_scholar"],
    # Astronomy / Astrophysics
    "astronomy":   ["nasa_ads", "nasa", "arxiv", "semantic_scholar", "crossref"],
    # Philosophy
    "philosophy":  ["sep", "crossref", "semantic_scholar", "wikidata_geo"],
    # Sociology / Social Sciences
    "sociology":   ["social_sciences", "crossref", "semantic_scholar", "newsapi"],
    # Psychology
    "psychology":  ["psychology", "pubmed", "crossref", "semantic_scholar"],
    # Religion / Theology
    "religion":    ["religion", "crossref", "wikidata_geo", "sep"],
    # Nutrition / Food Science
    "nutrition":   ["tavily", "nutrition", "pubmed", "europe_pmc", "crossref"],
    # Business / Finance / Management
    "business":    ["business", "world_bank", "crossref", "newsapi"],
    # Ethics / Moral Philosophy
    "ethics":      ["ethics", "sep", "crossref", "semantic_scholar"],
    # General fallback -- Wikipedia is LAST, not first
    "general":     ["tavily", "britannica", "semantic_scholar", "crossref",
                    "factcheck", "newsapi", "wikidata_geo"],
}


async def search_counter_evidence(claim: str) -> list[Source]:
    """
    Dedicated counter-evidence search.
    Searches specifically for debunking, myths, corrections, and refutations.
    This is the key to catching tricky claims and partial truths.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        loop = asyncio.get_event_loop()
        kw   = extract_keywords(claim)[:80]

        # Queries specifically designed to find counter-evidence
        counter_queries = [
            f"{kw} myth debunked false",
            f"{kw} no evidence wrong",
            f"is it true {kw[:50]}",
            f"{kw} fact check",
        ]

        TRUSTED = ["britannica.com", "snopes.com", "factcheck.org",
                   "politifact.com", "reuters.com", "bbc.com",
                   "apnews.com", "pubmed.ncbi.nlm.nih.gov",
                   "sciencedirect.com", "who.int", "cdc.gov"]

        def _search():
            results = []
            seen = set()
            with DDGS() as ddgs:
                for q in counter_queries[:3]:
                    for r in ddgs.text(q, max_results=5):
                        url = r.get("href", "")
                        if url not in seen:
                            seen.add(url)
                            results.append(r)
                    if len(results) >= 10:
                        break
            return results

        hits = await asyncio.wait_for(
            loop.run_in_executor(None, _search), timeout=12.0
        )

        sources = []
        for h in hits:
            url   = h.get("href", "")
            title = h.get("title", "")[:120]
            body  = h.get("body", "")[:400]
            if not title or not body:
                continue
            # Prioritize trusted counter-evidence sources
            if not any(d in url for d in TRUSTED):
                continue
            if any(w in (title + body).lower() for w in
                   ["myth", "false", "debunk", "wrong", "no evidence",
                    "not true", "mislead", "fact check", "actually"]):
                sources.append(Source(
                    type="factcheck",
                    title=title,
                    url=url,
                    snippet=body,
                    publisher=_domain(url),
                ))

        print(f"  [COUNTER] {len(sources)} counter-evidence sources found")
        return sources[:4]

    except Exception as e:
        print(f"  [COUNTER] error: {e}")
        return []


def build_source_plan(claim: str, topic: str):
    """
    Select the right sources based on detected topic.
    Always includes: Wikipedia, Fact-check (if key available), News RSS.
    Adds domain-specific sources on top.
    Max ~6-8 sources total for quality over quantity.
    """
    # Map topic to domain key
    # All topics map to themselves (DOMAIN_SOURCES is the authority)
    topic_map = {t: t for t in DOMAIN_SOURCES}
    # Common aliases
    topic_map.update({
        "science":         "physics",
        "math":            "mathematics",
        "tech":            "cs_tech",
        "law":             "politics",
        "social sciences": "sociology",
        "social science":  "sociology",
        "moral":           "ethics",
        "theology":        "religion",
        "astrophysics":    "astronomy",
        "food":            "nutrition",
        "diet":            "nutrition",
        "finance":         "economics",
        "company":         "business",
    })
    domain = topic_map.get(topic, "general")

    # Detect sub-domains from keywords
    c = claim.lower()
    # Let smart_detect_topic (Gemini) handle classification -- topic is already correct
    # These overrides are for edge cases when keyword fallback is used
    if any(w in c for w in ["element","compound","molecule","chemistry","chemical","reaction","acid"]):
        domain = "chemistry"
    elif any(w in c for w in ["theorem","equation","calculus","algebra","geometry","prime","math"]):
        domain = "mathematics"
    elif any(w in c for w in ["novel","poem","author","writer","literature","book","shakespeare"]):
        domain = "literature"
    elif any(w in c for w in ["painting","sculpture","architecture","museum","artist","film","cinema"]):
        domain = "art"
    elif any(w in c for w in ["football","soccer","basketball","player","team","olympic","sport"]):
        domain = "sports"
    elif any(w in c for w in ["algorithm","software","programming","ai","machine learning","computer"]):
        domain = "cs_tech"
    elif any(w in c for w in ["engineering","bridge","circuit","turbine","semiconductor"]):
        domain = "engineering"
    elif any(w in c for w in ["mountain","peak","river","country","capital","geography","vârf","munte"]):
        domain = "geography"
    elif any(w in c for w in ["climate","carbon","global warming","emission","drought","flood"]):
        domain = "climate"
    elif any(w in c for w in ["dna","gene","species","evolution","ecosystem","bacteria","biology"]):
        domain = "biology"
    selected = DOMAIN_SOURCES.get(domain, DOMAIN_SOURCES["general"])
    tasks, labels = [], []

    source_fns = {
        "pubmed":           lambda: search_pubmed(claim),
        "europe_pmc":       lambda: search_europe_pmc(claim),
        "arxiv":            lambda: search_arxiv(claim),
        "semantic_scholar": lambda: search_semantic_scholar(claim),
        "crossref":         lambda: search_crossref(claim),
        "wikidata":         lambda: search_wikidata(claim),
        "wikipedia":        lambda: search_wikipedia(claim),
        "ddg_wiki":         lambda: search_ddg_wiki(claim),
        "britannica":       lambda: search_britannica(claim),
        "tavily":           lambda: search_tavily(claim),
        "geonames":         lambda: search_geonames(claim),
        "rest_countries":   lambda: search_rest_countries(claim),
        "wikidata_geo":     lambda: search_wikidata_geo(claim),
        "pubchem":          lambda: search_pubchem(claim),
        "open_library":     lambda: search_open_library(claim),
        "met_museum":       lambda: search_met_museum(claim),
        "smithsonian":      lambda: search_smithsonian(claim),
        "nominatim":        lambda: search_nominatim(claim),
        "unesco":           lambda: search_unesco(claim),
        "harvard_art":      lambda: search_harvard_art(claim),
        "nps":              lambda: search_nps(claim),
        "historic_england": lambda: search_historic_england(claim),
        "usgs":             lambda: search_usgs(claim),
        "loc":              lambda: search_loc(claim),
        "epa":              lambda: search_epa(claim),
        "unesco_data":      lambda: search_unesco_data(claim),
        "who":              lambda: search_who(claim),
        "cdc":              lambda: search_cdc(claim),
        "clinicaltrials":   lambda: search_clinicaltrials(claim),
        "ncbi":             lambda: search_ncbi(claim),
        # Math
        "wolfram":          lambda: search_wolfram(claim),
        "openalex_math":    lambda: search_openalex_math(claim),
        # Sports
        "football_data":    lambda: search_football_data(claim),
        "nba_stats":        lambda: search_nba_stats(claim),
        "sportsdb":         lambda: search_sportsdb(claim),
        "f1":               lambda: search_f1(claim),
        # Politics
        "eu_data":          lambda: search_eu_data(claim),
        "govtrack":         lambda: search_govtrack(claim),
        "openstates":       lambda: search_openstates(claim),
        # New domains
        "sep":              lambda: search_sep(claim),
        "nasa_ads":         lambda: search_nasa_ads(claim),
        "psychology":       lambda: search_psychology(claim),
        "social_sciences":  lambda: search_social_sciences(claim),
        "religion":         lambda: search_religion(claim),
        "nutrition":        lambda: search_nutrition(claim),
        "business":         lambda: search_business(claim),
        "ethics":           lambda: search_ethics(claim),
        "news_rss":         lambda: search_news_rss(claim),
        "factcheck":        lambda: search_google_factcheck(claim),
        "europeana":        lambda: search_europeana(claim),
        # New authoritative sources
        "openfda":          lambda: search_openfda(claim),
        "world_bank":       lambda: search_world_bank(claim),
        "imf":              lambda: search_imf(claim),
        "oecd":             lambda: search_oecd(claim),
        "noaa":             lambda: search_noaa(claim),
        "gdelt":            lambda: search_gdelt_events(claim),
        "newsapi":          lambda: search_newsapi(claim),
        "guardian":         lambda: search_guardian(claim),
        "nasa":             lambda: search_nasa(claim),
        "openfda":          lambda: search_openfda_v2(claim),   # v2 with key
        "noaa":             lambda: search_noaa_v2(claim),      # v2 with token
    }

    for src in selected:
        if src in source_fns:
            tasks.append(source_fns[src]())
            labels.append(src.upper())

    # ── Temporal claims: prioritize news sources ──────────────
    if is_temporal_claim(claim):
        print(f"  [TEMPORAL] Claim requires recent sources -- boosting news APIs")
        if "TAVILY" not in labels and TAVILY_API_KEY:
            tasks.insert(0, search_tavily(claim))
            labels.insert(0, "TAVILY_TEMPORAL")
        if "NEWSAPI" not in labels and NEWS_API_KEY:
            tasks.insert(1, search_newsapi(claim))
            labels.insert(1, "NEWSAPI_TEMPORAL")
        if "GUARDIAN" not in labels and GUARDIAN_API_KEY:
            tasks.insert(2, search_guardian(claim))
            labels.insert(2, "GUARDIAN_TEMPORAL")
        if "GDELT" not in labels:
            tasks.insert(3, search_gdelt_events(claim))
            labels.insert(3, "GDELT_TEMPORAL")

    # Add Scopus only for academic domains
    academic_domains = {"medical","science","cs_tech","math","biology","climate","history"}
    if os.getenv("SCOPUS_API_KEY") and domain in academic_domains:
        tasks.append(search_scopus(claim))
        labels.append("SCOPUS")

    return tasks, labels


def _strip_diacritics(text: str) -> str:
    """Remove Romanian/accented characters for better search."""
    replacements = {
        'ă':'a','â':'a','î':'i','ș':'s','ț':'t','ş':'s','ţ':'t',
        'Ă':'A','Â':'A','Î':'I','Ș':'S','Ț':'T','Ş':'S','Ţ':'T',
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def _build_search_query(claim: str) -> str:
    """Build English search query: strip diacritics, remove stop/opinion words, keep entities."""
    STOP = {
        # English
        "the","a","an","is","are","was","were","has","have","that","this","it","its",
        "and","or","but","in","on","at","to","of","for","with","by","from","not","no",
        "be","been","being","do","did","does","can","could","would","will","shall",
        "best","worst","greatest","most","least","all","very","just","more","less","ever",
        "world","global","international","national","first","last","new","old","big","small",
        # Romanian
        "nu","ca","si","sau","dar","un","o","cel","cea","lui","este","sunt","care","din",
        "pentru","prin","despre","după","înainte","între","mai","mult","mult","puțin",
        "cel","mai","cel mai","din","lume","tara","tarii","acesta","aceasta","această",
        # Romanian geographic noise words
        "varful","varf","deal","munte","munti","mare","mic","lung","scurt","inalt","intalt",
        "adanc","lat","ingust","vechi","nou","principal","secundar","important",
    }
    clean = _strip_diacritics(claim)
    # Named entities: 2+ capitalized words (Lionel Messi, Great Wall)
    entities = re.findall(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+\b", clean)
    # Single capitalized words
    caps = re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", clean)
    # Lowercase content words (4+ chars)
    lowers = re.findall(r"\b[a-z]{4,}\b", clean.lower())

    result = []
    seen = set()

    for e in entities[:2]:
        key = e.lower()
        if not any(w in STOP for w in key.split()):
            result.append(e)
            for w in key.split(): seen.add(w)

    for w in caps[:3]:
        k = w.lower()
        if k not in STOP and k not in seen:
            result.append(w); seen.add(k)

    for w in lowers[:4]:
        if w not in STOP and w not in seen:
            result.append(w); seen.add(w)

    q = " ".join(result[:6]).strip()
    if not q:
        # Last resort: just take non-stop words from original
        words = [w for w in re.findall(r"[A-Za-z]{3,}", clean)
                 if w.lower() not in STOP]
        q = " ".join(words[:4])
    return q[:150]



async def split_claims(text: str) -> list[str]:
    """Split a compound claim into individual verifiable sub-claims using Gemini."""
    if not gemini_client: return [text]
    # Only split if text has multiple clauses
    if len(text.split()) < 8 or not any(c in text for c in [" și ", " and ", ",", ";"]):
        return [text]
    try:
        import asyncio as _asyncio, json as _json
        prompt = (
            'Split this into individual verifiable claims (max 4). '
            'Return JSON array of strings only: ["claim1","claim2"]\n\n'
            f'Text: "{text[:300]}"'
        )

        loop = _asyncio.get_event_loop()
        resp = await loop.run_in_executor(None,
            lambda: gemini_client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt,
                                config=make_gemini_config(max_tokens=200, use_search=False, thinking_budget=0)))
        raw = resp.text.strip().replace("```json","").replace("```","").strip()
        s, e2 = raw.find("["), raw.rfind("]")
        if s != -1 and e2 > s:
            claims = _json.loads(raw[s:e2+1])
            if isinstance(claims, list) and all(isinstance(c,str) for c in claims):
                claims = [c for c in claims if len(c.strip()) > 5]
                if len(claims) > 1:
                    print(f"  [SPLIT] {len(claims)} sub-claims found")
                    return claims[:4]
    except Exception as e:
        print(f"  [SPLIT] Error: {e}")
    return [text]


def compute_word_importance(claim: str, verdict: str, score: int) -> list[dict]:
    """
    Compute word importance using linguistic heuristics.
    Returns list of {word, importance, direction} dicts.
    """
    if not claim or not verdict or verdict == "UNCERTAIN":
        return []

    words = re.findall(r"[a-zA-Zăâîșț]{3,}", claim.lower())
    stop = {"the","and","for","with","that","this","from","are","was","were",
            "has","have","been","not","can","but","its","the","este","sunt",
            "care","care","prin","din","sau","dar","ori","cel","mai","ale"}
    words = [w for w in words if w not in stop]

    if not words:
        return []

    # Score words by their factual significance
    NUMBER_PATTERN = re.compile(r'\d')
    result = []
    for word in list(dict.fromkeys(words))[:12]:  # unique, max 12
        imp = 0.0
        # Numbers are high-importance (dates, stats, measurements)
        if NUMBER_PATTERN.search(word): imp += 0.4
        # Named entities (capitalized in original) are high-importance
        if any(w == word or w.lower() == word for w in claim.split() if w and w[0].isupper()):
            imp += 0.35
        # Domain-specific words
        if word in ["ph","dna","rna","co2","adn","arn","pib","gdp","nato","ue","eu"]:
            imp += 0.3
        # Common factual words
        if word in ["caused","causes","discovered","invented","founded","born","died",
                    "won","lost","largest","smallest","highest","lowest","first","last",
                    "cauzat","descoperit","inventat","fondat","castigat","primul","primul"]:
            imp += 0.25
        # Superlatives / comparatives
        if word in ["most","best","worst","largest","highest","lowest","biggest",
                    "cel","mai","cel mai","mult","putin","mare","mic","inalt"]:
            imp += 0.2
        # Base importance for meaningful words
        imp += 0.1

        direction = "supporting" if verdict == "TRUE" else "contradicting" if imp > 0.3 else "neutral"
        result.append({
            "word": word,
            "importance": round(min(imp, 1.0), 3),
            "direction": direction
        })

    return sorted(result, key=lambda x: x["importance"], reverse=True)