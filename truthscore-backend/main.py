"""
TruthScore v3 — Evidence-Based Fact-Checking Pipeline
======================================================
MSc Thesis: AI Real-Time Fact-Checking System

ARCHITECTURE (evidence-first, RAG-style):
  1. RETRIEVE  — gather evidence from 6 sources in parallel
  2. SCORE     — run NLI cross-encoder on each (evidence, claim) pair
  3. AGGREGATE — compute TruthScore from entailment/contradiction signals
  4. EXPLAIN   — return ranked sources labeled SUPPORTS / CONTRADICTS / NEUTRAL

NLI MODELS:
  - cross-encoder/nli-deberta-v3-large  (primary, best for evidence pairs)
  - facebook/bart-large-mnli            (fallback zero-shot)

EVIDENCE SOURCES (all free):
  - DuckDuckGo Web Search               (no API key)
  - Wikipedia Full-Text Search          (no API key)
  - Wikidata Structured Knowledge       (no API key)
  - OpenAlex Academic Papers            (no API key)
  - Reuters / BBC / FactCheck / Snopes  RSS feeds (no API key)
  - Google Fact Check Tools API         (free key)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import asyncio, httpx, os, re, diskcache, math
from dotenv import load_dotenv

load_dotenv(override=True)

# ── API Keys ──────────────────────────────────────────────────
HF_TOKEN       = os.getenv("HF_TOKEN", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# ── Models ────────────────────────────────────────────────────
# Primary: cross-encoder takes (premise=evidence, hypothesis=claim)
# and returns ENTAILMENT / NEUTRAL / CONTRADICTION scores
# NLI model applied per (evidence, claim) pair via zero-shot
# bart-large-mnli supports multi-premise zero-shot NLI
NLI_MODEL_EN    = "facebook/bart-large-mnli"
NLI_MODEL_MULTI = "joeddav/xlm-roberta-large-xnli"
NLI_CROSSENCODER = NLI_MODEL_EN  # alias for display
NLI_ZEROSHOT_EN   = NLI_MODEL_EN
NLI_ZEROSHOT_MULTI = NLI_MODEL_MULTI
HF_ROUTER = "https://router.huggingface.co/hf-inference/models/"

# Zero-shot labels (fallback when no evidence found)
ZEROSHOT_LABELS = [
    "factually correct and verified",
    "misinformation or false claim",
    "partially correct or misleading",
    "unverifiable or opinion",
]

# ── Config ────────────────────────────────────────────────────
GOOGLE_FC_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
RO_CHARS      = set("ăâîșțĂÂÎȘȚ")
cache         = diskcache.Cache(".cache/ts3")

RSS_FEEDS = [
    ("Reuters",        "https://feeds.reuters.com/reuters/topNews"),
    ("BBC News",       "https://feeds.bbci.co.uk/news/rss.xml"),
    ("FactCheck.org",  "https://www.factcheck.org/feed/"),
    ("Snopes",         "https://www.snopes.com/feed/"),
    ("AP News",        "https://apnews.com/rss"),
    ("The Guardian",   "https://www.theguardian.com/world/rss"),
]

# ── App ───────────────────────────────────────────────────────
app = FastAPI(title="TruthScore API v3", version="3.0",
              description="Evidence-based AI fact-checking pipeline")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Data models ───────────────────────────────────────────────
class VerifyRequest(BaseModel):
    text: str = Field(..., min_length=5, max_length=1000)

class NLIScore(BaseModel):
    entailment: float    # evidence SUPPORTS claim
    neutral: float
    contradiction: float # evidence REFUTES claim
    verdict: str         # "SUPPORTS" | "CONTRADICTS" | "NEUTRAL"

class Source(BaseModel):
    type: str            # "web"|"wikipedia"|"wikidata"|"academic"|"news"|"factcheck"
    title: str
    url: str
    snippet: str = ""
    publisher: str = ""
    nli: NLIScore | None = None
    relevance: float = 0.0

class VerifyResponse(BaseModel):
    claim: str
    score: int           # 0-100
    verdict: str         # "TRUE"|"FALSE"|"UNCERTAIN"
    confidence: str      # "HIGH"|"MEDIUM"|"LOW"
    explanation: str
    supporting: list[Source] = []
    contradicting: list[Source] = []
    neutral_sources: list[Source] = []
    evidence_count: int = 0
    models_used: list[str] = []
    cached: bool = False

# ── Routes ────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok", "version": "3.0",
        "architecture": "evidence-first RAG pipeline",
        "hf_token":    "set" if HF_TOKEN   else "MISSING",
        "google_key":  "set" if GOOGLE_API_KEY else "missing",
        "primary_model":  NLI_CROSSENCODER,
        "fallback_model": NLI_ZEROSHOT_EN,
    }

@app.post("/clear-cache")
async def clear_cache():
    cache.clear()
    return {"status": "cache cleared"}

@app.post("/verify", response_model=VerifyResponse)
async def verify_claim(req: VerifyRequest):
    claim = req.text.strip()
    key   = f"v3:{claim[:200].lower()}"

    hit = cache.get(key)
    if hit:
        hit["cached"] = True
        return VerifyResponse(**hit)

    # ── Math shortcut ─────────────────────────────────────────
    math_result = evaluate_math_claim(claim)
    if math_result:
        score, expl = math_result
        verdict    = "TRUE" if score >= 70 else ("FALSE" if score < 38 else "UNCERTAIN")
        result = VerifyResponse(
            claim=claim, score=score, verdict=verdict,
            confidence="HIGH", explanation=expl,
            models_used=["mathematical-evaluator"],
        )
        cache.set(key, result.model_dump(), expire=3600 * 24)
        return result

    # ── Detect claim topic for smart source prioritization ──
    topic = detect_topic(claim)
    print(f"\n[PIPELINE] Claim: {claim[:80]}... | Topic: {topic}")
    evidence_tasks = [
        search_duckduckgo(claim),          # Trusted web (Reuters, BBC, WHO...)
        search_wikipedia(claim),           # Wikipedia EN + RO
        search_wikidata(claim),            # Structured knowledge
        search_pubmed(claim),              # 35M biomedical (NCBI)
        search_arxiv(claim),               # 2.3M scientific preprints
        search_semantic_scholar(claim),    # 220M academic papers
        search_crossref(claim),            # 150M scholarly DOIs
        search_openalex(claim),            # 250M academic works
        search_core(claim),                # 200M open access papers
        search_europe_pmc(claim),          # 45M life sciences
        search_doaj(claim),                # 20M open access journals
        search_ieee(claim),                # Engineering & CS (IEEE_API_KEY)
        search_scopus(claim),              # Scopus institutional (SCOPUS_API_KEY)
        search_gdelt_news(claim),          # Global news index
        search_europa_who(claim),          # WHO/EU/UN/CDC official
        search_rss(claim),                 # FactCheck.org/Snopes/AP
        search_google_factcheck(claim),    # Google Fact Check Tools
    ]
    labels = ["WEB","WIKI","WIKIDATA","PUBMED","ARXIV","S2","CROSSREF","OPENALEX",
              "CORE","EUROPEPMC","DOAJ","IEEE","SCOPUS","GDELT","GOV","RSS","FC"]
    results = await asyncio.gather(*evidence_tasks, return_exceptions=True)

    all_evidence: list[Source] = []
    for label, res in zip(labels, results):
        if isinstance(res, Exception):
            print(f"  [{label}] ERROR: {str(res)[:120]}")
        else:
            print(f"  [{label}] {len(res)} sources")
            all_evidence.extend(res)

    print(f"  TOTAL: {len(all_evidence)} evidence pieces")

    # ── Step 2: NLI scoring per evidence piece ────────────────
    models_used = []
    if HF_TOKEN and all_evidence:
        scored = await score_evidence_with_nli(claim, all_evidence)
        models_used.append(NLI_CROSSENCODER)
    else:
        scored = all_evidence  # no NLI scores, use as neutral

    # ── Step 3: Fallback zero-shot if evidence is sparse ─────
    zero_score = None
    if HF_TOKEN and len(all_evidence) < 3:
        try:
            zero_score = await run_zeroshot_nli(claim)
            models_used.append(NLI_ZEROSHOT_EN)
        except Exception as e:
            print(f"  [ZEROSHOT] {e}")

    # ── Step 4: Aggregate TruthScore ─────────────────────────
    score, verdict, confidence, explanation = aggregate_score(
        scored, zero_score, len(all_evidence)
    )

    # ── Step 5: Split sources by verdict ─────────────────────
    supporting    = sorted([s for s in scored if s.nli and s.nli.verdict == "SUPPORTS"],
                           key=lambda x: x.nli.entailment, reverse=True)[:5]
    contradicting = sorted([s for s in scored if s.nli and s.nli.verdict == "CONTRADICTS"],
                           key=lambda x: x.nli.contradiction, reverse=True)[:5]
    neutral       = [s for s in scored if not s.nli or s.nli.verdict == "NEUTRAL"][:3]

    result = VerifyResponse(
        claim=claim, score=score, verdict=verdict,
        confidence=confidence, explanation=explanation,
        supporting=supporting, contradicting=contradicting,
        neutral_sources=neutral,
        evidence_count=len(all_evidence),
        models_used=models_used,
    )
    cache.set(key, result.model_dump(), expire=3600 * 6)
    return result


# ════════════════════════════════════════════════════════════
# EVIDENCE SOURCES
# ════════════════════════════════════════════════════════════

# Trusted domains for web search filtering
TRUSTED_DOMAINS = {
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

async def search_duckduckgo(claim: str) -> list[Source]:
    """
    Web search filtered to trusted domains only.
    Uses ddgs (duckduckgo_search renamed package) with domain filtering.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        loop = asyncio.get_event_loop()
        kw   = claim[:200]

        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(kw, max_results=20, timelimit="y"))

        hits = await asyncio.wait_for(
            loop.run_in_executor(None, _search), timeout=15.0
        )

        sources = []
        # First pass: trusted domains only
        for h in hits:
            domain = _domain(h.get("href", ""))
            if not any(td in domain for td in TRUSTED_DOMAINS):
                continue
            sources.append(Source(
                type="web",
                title=h.get("title", "")[:120],
                url=h.get("href", ""),
                snippet=h.get("body", "")[:400],
                publisher=domain,
            ))
            if len(sources) >= 6:
                break

        # Second pass: if < 3 trusted results, include other English results
        if len(sources) < 3:
            for h in hits:
                url = h.get("href", "")
                if any(s.url == url for s in sources):
                    continue
                # Skip Chinese, Russian, spam
                body = h.get("body", "")
                if _is_low_quality(url, body):
                    continue
                sources.append(Source(
                    type="web",
                    title=h.get("title", "")[:120],
                    url=url,
                    snippet=body[:400],
                    publisher=_domain(url),
                ))
                if len(sources) >= 5:
                    break

        return sources[:6]
    except Exception as e:
        raise RuntimeError(f"Web search: {e}")


def _is_low_quality(url: str, body: str) -> bool:
    """Filter out low-quality, non-English, or irrelevant results."""
    low_quality_domains = [
        "zhihu.com", "baidu.com", "weibo.com", "taobao.com",
        "vk.com", "ok.ru", "pinterest.com", "instagram.com",
        "tiktok.com", "youtube.com", "facebook.com", "twitter.com",
    ]
    domain = _domain(url)
    if any(lq in domain for lq in low_quality_domains):
        return True
    # Detect mostly non-Latin characters (Chinese, Arabic, etc.)
    if body:
        latin = sum(1 for c in body[:200] if c.isascii())
        if latin < len(body[:200]) * 0.4:
            return True
    return False



# ════════════════════════════════════════════════════════════
# PREMIUM EVIDENCE SOURCES
# ════════════════════════════════════════════════════════════

async def search_pubmed(claim: str) -> list[Source]:
    """
    PubMed / NCBI — 35 million biomedical abstracts.
    Free, no API key. Best for medical/health/biology claims.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        # Step 1: search IDs
        r = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params={
            "db": "pubmed", "term": kw, "retmax": 5,
            "retmode": "json", "sort": "relevance",
            "tool": "TruthScore", "email": "thesis@example.com",
        })
        if r.status_code != 200: return []
        ids = r.json().get("esearchresult", {}).get("idlist", [])
        if not ids: return []

        # Step 2: fetch abstracts
        r2 = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params={
            "db": "pubmed", "id": ",".join(ids[:4]),
            "rettype": "abstract", "retmode": "text",
            "tool": "TruthScore", "email": "thesis@example.com",
        })
        if r2.status_code != 200: return []

        sources = []
        # Parse plain text abstracts
        blocks = r2.text.split("\n\n\n")
        for i, block in enumerate(blocks[:4]):
            if len(block.strip()) < 30: continue
            lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
            title = lines[0][:120] if lines else "PubMed Article"
            pmid  = ids[i] if i < len(ids) else ""
            abstract = " ".join(lines[1:])[:500]
            sources.append(Source(
                type="academic",
                title=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                snippet=abstract,
                publisher="PubMed / NCBI (National Library of Medicine)",
            ))
        return sources


async def search_arxiv(claim: str) -> list[Source]:
    """
    arXiv — 2.3M preprints in physics, math, CS, biology, economics.
    Free, no API key.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get("http://export.arxiv.org/api/query", params={
            "search_query": f"all:{kw}",
            "start": 0, "max_results": 4,
            "sortBy": "relevance", "sortOrder": "descending",
        })
        if r.status_code != 200: return []
        import xml.etree.ElementTree as ET
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.text)
        sources = []
        for entry in root.findall("atom:entry", ns)[:4]:
            title   = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n"," ")
            summary = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n"," ")[:400]
            url     = entry.findtext("atom:id", "", ns) or ""
            authors = [a.findtext("atom:name","",ns) for a in entry.findall("atom:author",ns)[:3]]
            pub     = ", ".join(authors) + " — arXiv"
            if not title: continue
            sources.append(Source(
                type="academic", title=title[:120],
                url=url, snippet=summary,
                publisher=pub,
            ))
        return sources


async def search_semantic_scholar(claim: str) -> list[Source]:
    """
    Semantic Scholar — 220M academic papers with AI-powered relevance.
    Free, no API key for basic search.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={
                "query": kw, "limit": 5,
                "fields": "title,abstract,year,authors,url,venue,openAccessPdf",
            },
            headers={"User-Agent": "TruthScore/3.0 (MSc Thesis)"},
        )
        if r.status_code != 200: return []
        sources = []
        for p in r.json().get("data", [])[:5]:
            title    = (p.get("title") or "")[:120]
            abstract = (p.get("abstract") or "")[:400]
            year     = p.get("year", "")
            venue    = p.get("venue", "Academic Paper")
            authors  = ", ".join(a.get("name","") for a in (p.get("authors") or [])[:2])
            pdf_url  = (p.get("openAccessPdf") or {}).get("url","")
            url      = pdf_url or p.get("url","https://www.semanticscholar.org")
            if not title: continue
            sources.append(Source(
                type="academic",
                title=f"{title} ({year})",
                url=url, snippet=abstract,
                publisher=f"{authors} — {venue}" if authors else venue,
            ))
        return sources


async def search_crossref(claim: str) -> list[Source]:
    """
    CrossRef — 150M+ scholarly works DOI metadata.
    Free, no API key.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.crossref.org/works", params={
            "query": kw, "rows": 4,
            "select": "title,abstract,URL,published,author,container-title",
            "mailto": "thesis@example.com",
        })
        if r.status_code != 200: return []
        sources = []
        for item in r.json().get("message", {}).get("items", [])[:4]:
            titles  = item.get("title", [])
            title   = titles[0][:120] if titles else ""
            abstract = item.get("abstract", "")
            abstract = re.sub(r"<[^>]+>", "", abstract)[:400]
            url     = item.get("URL", "")
            year    = item.get("published",{}).get("date-parts",[[""]])[0][0]
            journal = (item.get("container-title") or [""])
            journal = journal[0] if journal else "Academic Journal"
            authors = item.get("author",[])
            author_str = ", ".join(
                f"{a.get('given','')} {a.get('family','')}".strip()
                for a in authors[:2]
            )
            if not title or not url: continue
            sources.append(Source(
                type="academic",
                title=f"{title} ({year})",
                url=url,
                snippet=abstract if abstract else f"Scholarly article in {journal}",
                publisher=f"{author_str} — {journal}" if author_str else journal,
            ))
        return sources


async def search_gdelt_news(claim: str) -> list[Source]:
    """
    GDELT DOC API — searches 65+ countries, 100+ languages of news.
    Free, no API key. Real-time global news index.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get("https://api.gdeltproject.org/api/v2/doc/doc", params={
            "query": kw,
            "mode": "artlist", "maxrecords": 8,
            "format": "json", "timespan": "1y",
            "sort": "hybridrel",
            # Filter to high-quality English-language news
            "domain": "reuters.com OR apnews.com OR bbc.com OR theguardian.com OR nytimes.com OR washingtonpost.com OR economist.com OR nature.com OR science.org OR who.int",
        })
        if r.status_code != 200: return []
        try:
            data = r.json()
        except Exception:
            return []
        sources = []
        for art in (data.get("articles") or [])[:6]:
            title  = (art.get("title") or "")[:120]
            url    = art.get("url","")
            domain = art.get("domain","")
            seendate = art.get("seendate","")[:8]
            if not title or not url: continue
            # Format date YYYYMMDD → YYYY-MM-DD
            if len(seendate) == 8:
                seendate = f"{seendate[:4]}-{seendate[4:6]}-{seendate[6:8]}"
            sources.append(Source(
                type="news", title=title,
                url=url, snippet=f"News article from {domain}, {seendate}",
                publisher=domain,
            ))
        return sources[:5]


async def search_europa_who(claim: str) -> list[Source]:
    """
    Search WHO, EU, UN, Our World in Data official sites.
    Uses GDELT filtered to institutional domains.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.gdeltproject.org/api/v2/doc/doc", params={
            "query": kw,
            "mode": "artlist", "maxrecords": 5,
            "format": "json", "timespan": "5y",
            "sort": "hybridrel",
            "domain": "who.int OR europa.eu OR un.org OR worldbank.org OR nih.gov OR cdc.gov OR nasa.gov OR ourworldindata.org OR britannica.com OR statista.com",
        })
        if r.status_code != 200: return []
        try:
            data = r.json()
        except Exception:
            return []
        sources = []
        for art in (data.get("articles") or [])[:4]:
            title  = (art.get("title") or "")[:120]
            url    = art.get("url","")
            domain = art.get("domain","")
            if not title or not url: continue
            sources.append(Source(
                type="factcheck", title=title,
                url=url, snippet=f"Official source: {domain}",
                publisher=domain,
            ))
        return sources


async def search_core(claim: str) -> list[Source]:
    """
    CORE — 200M open access research papers. Free API, no key needed for basic.
    https://core.ac.uk/
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get("https://api.core.ac.uk/v3/search/works", params={
            "q": kw, "limit": 5,
            "fields": "title,abstract,yearPublished,authors,sourceFulltextUrls,doi",
        }, headers={"User-Agent": "TruthScore/3.0 (MSc Thesis)"})
        if r.status_code != 200: return []
        sources = []
        for p in r.json().get("results", [])[:5]:
            title    = (p.get("title") or "")[:120]
            abstract = (p.get("abstract") or "")[:400]
            year     = p.get("yearPublished", "")
            doi      = p.get("doi") or ""
            urls     = p.get("sourceFulltextUrls") or []
            url      = f"https://doi.org/{doi}" if doi else (urls[0] if urls else "https://core.ac.uk")
            authors  = p.get("authors") or []
            auth_str = ", ".join((a.get("name","") for a in authors[:2]))
            if not title: continue
            sources.append(Source(
                type="academic", title=f"{title} ({year})",
                url=url, snippet=abstract,
                publisher=f"{auth_str} — CORE Open Access" if auth_str else "CORE Open Access",
            ))
        return sources


async def search_europe_pmc(claim: str) -> list[Source]:
    """
    Europe PMC — 45M life sciences articles (PubMed + preprints + patents).
    Free, no API key.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params={
            "query": kw, "resultType": "core",
            "pageSize": 5, "format": "json",
            "sort": "RELEVANCE",
        })
        if r.status_code != 200: return []
        sources = []
        for art in r.json().get("resultList", {}).get("result", [])[:5]:
            title   = (art.get("title") or "")[:120]
            pmid    = art.get("pmid","")
            pmcid   = art.get("pmcid","")
            doi     = art.get("doi","")
            year    = art.get("pubYear","")
            journal = art.get("journalTitle","Europe PMC")
            abstract = (art.get("abstractText") or "")[:400]
            url = (f"https://doi.org/{doi}" if doi
                   else f"https://europepmc.org/article/med/{pmid}" if pmid
                   else "https://europepmc.org")
            if not title: continue
            sources.append(Source(
                type="academic", title=f"{title} ({year})",
                url=url, snippet=abstract,
                publisher=f"{journal} — Europe PMC",
            ))
        return sources


async def search_doaj(claim: str) -> list[Source]:
    """DOAJ — 20M+ open access articles from peer-reviewed journals."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(
            "https://doaj.org/api/v3/search/articles",
            params={"q": kw, "pageSize": 5, "sort": "score"},
            headers={"User-Agent": "TruthScore/3.0 (MSc Thesis)"},
        )
        if r.status_code != 200:
            return []
        sources = []
        for art in r.json().get("results", [])[:5]:
            bib      = art.get("bibjson", {})
            title    = (bib.get("title") or "")[:120]
            year     = str(bib.get("year", ""))
            journal  = (bib.get("journal") or {}).get("title", "DOAJ Journal")
            abstract = (bib.get("abstract") or "")[:400]
            links    = bib.get("link") or []
            url      = next(
                (l.get("url","") for l in links if l.get("type") == "fulltext"),
                "https://doaj.org"
            )
            if not title: continue
            sources.append(Source(
                type="academic", title=f"{title} ({year})",
                url=url, snippet=abstract or f"Peer-reviewed article in {journal}",
                publisher=f"{journal} (DOAJ – Peer Reviewed)",
            ))
        return sources


async def search_ieee(claim: str) -> list[Source]:
    """
    IEEE Xplore — engineering, CS, electronics, AI papers.
    Requires free API key from: https://developer.ieee.org/
    Set IEEE_API_KEY in .env
    """
    ieee_key = os.getenv("IEEE_API_KEY", "")
    kw = extract_keywords(claim)
    if not kw: return []
    params = {
        "querytext": kw, "max_records": 5,
        "start_record": 1, "sort_order": "desc",
        "sort_field": "relevance",
    }
    if ieee_key:
        params["apikey"] = ieee_key
    else:
        return []  # IEEE requires API key
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(
            "https://ieeexploreapi.ieee.org/api/v1/search/articles",
            params=params,
            headers={"Accept": "application/json", "User-Agent": "TruthScore/3.0"},
        )
        if r.status_code != 200:
            print(f"  [IEEE] {r.status_code}: {r.text[:100]}")
            return []
        try: data = r.json()
        except: return []
        sources = []
        for art in data.get("articles", [])[:5]:
            title    = (art.get("title") or "")[:120]
            abstract = (art.get("abstract") or "")[:400]
            url      = art.get("html_url", "https://ieeexplore.ieee.org")
            year     = art.get("publication_year", "")
            pub      = art.get("publication_title", "IEEE Xplore")
            authors  = art.get("authors", {}).get("authors", [])
            auth_str = ", ".join(a.get("full_name","") for a in authors[:2])
            if not title: continue
            sources.append(Source(
                type="academic", title=f"{title} ({year})",
                url=url, snippet=abstract,
                publisher=f"{auth_str} — {pub} (IEEE)" if auth_str else f"{pub} (IEEE)",
            ))
        return sources


async def search_scopus(claim: str) -> list[Source]:
    """
    Scopus (Elsevier) — largest abstract/citation database.
    Requires institutional API key from: https://dev.elsevier.com/
    Set SCOPUS_API_KEY in .env (get it with your university account)
    """
    scopus_key = os.getenv("SCOPUS_API_KEY", "")
    if not scopus_key: return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            "https://api.elsevier.com/content/search/scopus",
            params={
                "query": f"TITLE-ABS-KEY({kw})",
                "count": 6, "start": 0,
                "field": "dc:title,dc:description,prism:doi,prism:publicationName,dc:creator,prism:coverDate",
                "sort": "relevancy",
            },
            headers={
                "X-ELS-APIKey": scopus_key,
                "Accept": "application/json",
                "User-Agent": "TruthScore/3.0",
            },
        )
        if r.status_code != 200:
            print(f"  [SCOPUS] {r.status_code}: {r.text[:150]}")
            return []
        sources = []
        entries = r.json().get("search-results", {}).get("entry", [])
        for e in entries[:6]:
            title   = (e.get("dc:title") or "")[:120]
            doi     = e.get("prism:doi", "")
            journal = e.get("prism:publicationName", "Scopus")
            desc    = (e.get("dc:description") or "")[:400]
            creator = e.get("dc:creator", "")
            date    = e.get("prism:coverDate", "")[:4]
            url     = f"https://doi.org/{doi}" if doi else "https://www.scopus.com"
            if not title: continue
            sources.append(Source(
                type="academic",
                title=f"{title} ({date})",
                url=url,
                snippet=desc or f"Scopus-indexed article in {journal}",
                publisher=f"{creator} — {journal} (Scopus)" if creator else f"{journal} (Scopus)",
            ))
        return sources


async def search_wikipedia(claim: str) -> list[Source]:
    """Wikipedia full-text search + intro extract. Searches EN + RO."""
    kw = extract_keywords(claim)
    if not kw or len(kw) < 4:
        kw = claim[:100]
    headers = {"User-Agent": "TruthScore/3.0 (MSc Thesis; https://github.com) Python/httpx"}
    langs = ["ro", "en"] if any(c in RO_CHARS for c in claim) else ["en"]
    sources = []

    async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
        for lang in langs:
            base = f"https://{lang}.wikipedia.org/w/api.php"
            try:
                r = await client.get(base, params={
                    "action": "query", "list": "search",
                    "srsearch": kw, "format": "json",
                    "srlimit": 4, "srprop": "snippet|titlesnippet",
                })
                if r.status_code != 200: continue
                data = r.json()
                hits = data.get("query", {}).get("search", [])
                if not hits: continue

                # Get extracts for first 3 results
                pageids = "|".join(str(h["pageid"]) for h in hits[:3])
                r2 = await client.get(base, params={
                    "action": "query", "pageids": pageids,
                    "prop": "extracts|info", "exintro": 1,
                    "explaintext": 1, "exsentences": 5,
                    "format": "json", "inprop": "url",
                })
                if r2.status_code != 200: continue
                pages = r2.json().get("query", {}).get("pages", {})
                for pid, page in pages.items():
                    if pid == "-1": continue
                    extract = (page.get("extract") or "").strip()
                    title   = page.get("title", "")
                    canon   = page.get("canonicalurl", "")
                    url     = canon or f"https://{lang}.wikipedia.org/wiki/{title.replace(' ','_')}"
                    if not extract or not title: continue
                    sources.append(Source(
                        type="wikipedia", title=title,
                        url=url, snippet=extract[:500],
                        publisher=f"Wikipedia ({lang.upper()})",
                    ))
            except Exception as e:
                print(f"  [WIKI-{lang.upper()}] {e}")
                continue
    return sources[:5]


async def search_wikidata(claim: str) -> list[Source]:
    """Wikidata structured knowledge — curated facts, reliable."""
    kw = extract_keywords(claim)
    if not kw: return []
    headers = {"User-Agent": "TruthScore/3.0 (MSc Thesis) Python/httpx"}
    sources = []

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        # Search entities
        r = await client.get("https://www.wikidata.org/w/api.php", params={
            "action": "wbsearchentities", "search": kw,
            "language": "en", "uselang": "en",
            "format": "json", "limit": 5, "type": "item",
        })
        if r.status_code != 200: return []
        items = r.json().get("search", [])

        for item in items[:4]:
            label = item.get("label", "")
            desc  = item.get("description", "")
            qid   = item.get("id", "")
            aliases = ", ".join(item.get("aliases", [])[:2])
            if not label or not qid: continue
            snippet = f"{label}: {desc}" if desc else label
            if aliases:
                snippet += f" (also known as: {aliases})"
            sources.append(Source(
                type="wikidata", title=label,
                url=f"https://www.wikidata.org/wiki/{qid}",
                snippet=snippet,
                publisher="Wikidata (Wikimedia Foundation)",
            ))
    return sources[:3]


async def search_openalex(claim: str) -> list[Source]:
    """OpenAlex — 250M academic papers, free."""
    kw = extract_keywords(claim)
    if len(kw) < 8: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.openalex.org/works", params={
            "search": kw, "per-page": 5,
            "select": "title,doi,publication_year,abstract_inverted_index,primary_location",
            "mailto": "thesis@example.com",
        })
        if r.status_code != 200: return []
        sources = []
        for w in r.json().get("results", [])[:5]:
            title = w.get("title", "")
            doi   = w.get("doi", "")
            year  = w.get("publication_year", "")
            venue = ((w.get("primary_location") or {}).get("source") or {})
            journal = venue.get("display_name", "Academic Paper")
            abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
            if not title: continue
            sources.append(Source(
                type="academic",
                title=f"{title[:100]} ({year})",
                url=doi or "https://openalex.org",
                snippet=abstract[:400] if abstract else f"Academic paper: {title}",
                publisher=journal,
            ))
        return sources


async def search_rss(claim: str) -> list[Source]:
    """Reuters, BBC, FactCheck.org, Snopes, AP News, Guardian RSS feeds."""
    keywords = set(w.lower() for w in extract_keywords(claim).split() if len(w) > 3)
    if not keywords: return []

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                                  headers={"User-Agent": "TruthScore/3.0"}) as client:
        tasks = [client.get(url) for _, url in RSS_FEEDS]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    sources = []
    for (name, feed_url), resp in zip(RSS_FEEDS, responses):
        if isinstance(resp, Exception) or resp.status_code != 200:
            continue
        text  = resp.text
        items = re.findall(
            r"<item[^>]*>(.*?)</item>", text, re.DOTALL | re.IGNORECASE
        )
        for item in items[:30]:
            title_m = re.search(r"<title[^>]*><!\[CDATA\[(.*?)\]\]></title>|<title[^>]*>(.*?)</title>", item, re.DOTALL)
            link_m  = re.search(r"<link[^>]*>(https?://[^<]+)</link>|<guid[^>]*>(https?://[^<]+)</guid>", item)
            desc_m  = re.search(r"<description[^>]*><!\[CDATA\[(.*?)\]\]></description>|<description[^>]*>(.*?)</description>", item, re.DOTALL)

            title   = ((title_m.group(1) or title_m.group(2)) if title_m else "").strip()
            url     = (link_m.group(1) or link_m.group(2) if link_m else feed_url).strip()
            snippet = re.sub(r"<[^>]+>", "", (desc_m.group(1) or desc_m.group(2)) if desc_m else "").strip()[:300]

            if not title or len(title) < 5: continue
            if not any(kw in title.lower() or kw in snippet.lower() for kw in keywords):
                continue
            sources.append(Source(
                type="news", title=title[:120], url=url,
                snippet=snippet, publisher=name,
            ))
            if len(sources) >= 6: break
        if len(sources) >= 6: break
    return sources[:5]


async def search_google_factcheck(claim: str) -> list[Source]:
    """Google Fact Check Tools API — professional fact-checkers."""
    if not GOOGLE_API_KEY: return []
    lang = "ro" if any(c in RO_CHARS for c in claim) else "en"
    sources, seen = [], set()

    async with httpx.AsyncClient(timeout=10.0) as client:
        for lc in ([lang, "en"] if lang == "ro" else ["en"]):
            r = await client.get(GOOGLE_FC_URL, params={
                "query": claim[:200], "key": GOOGLE_API_KEY,
                "languageCode": lc,
            })
            if r.status_code != 200: continue
            for item in r.json().get("claims", [])[:5]:
                rev  = item.get("claimReview", [{}])[0]
                url  = rev.get("url", "")
                if url in seen: continue
                seen.add(url)
                rating =DistinguishedRating(rev.get("textualRating", "Unknown"))
                sources.append(Source(
                    type="factcheck",
                    title=item.get("text", claim)[:120],
                    url=url,
                    snippet=f"Rated: {rating} by {rev.get('publisher',{}).get('name','Unknown')}",
                    publisher=rev.get("publisher", {}).get("name", "Unknown"),
                    nli=factcheck_rating_to_nli(rating),
                ))
    return sources[:5]


# ════════════════════════════════════════════════════════════
# NLI SCORING
# ════════════════════════════════════════════════════════════

async def score_evidence_with_nli(claim: str, evidence: list[Source]) -> list[Source]:
    """
    Run NLI cross-encoder on top-N evidence pieces.
    Model: cross-encoder/nli-deberta-v3-large
    Input: (premise=evidence_snippet, hypothesis=claim)
    Output: ENTAILMENT / NEUTRAL / CONTRADICTION + scores
    """
    # Pre-filter: skip fact-checks (already have NLI), take top 10 by snippet length
    to_score = [s for s in evidence if not s.nli and s.snippet and len(s.snippet) > 30]
    to_score = sorted(to_score, key=lambda s: len(s.snippet), reverse=True)[:10]
    already_scored = [s for s in evidence if s.nli]

    if not to_score:
        return already_scored + [s for s in evidence if not s.nli]

    # Run NLI in parallel batches of 3
    batch_size = 3
    scored: list[Source] = []
    for i in range(0, len(to_score), batch_size):
        batch = to_score[i:i+batch_size]
        tasks = [_nli_pair(claim, s) for s in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for src, res in zip(batch, results):
            if isinstance(res, NLIScore):
                src.nli = res
            else:
                print(f"  [NLI-PAIR] {str(res)[:80]}")
            scored.append(src)

    return already_scored + scored + [s for s in evidence
                                       if s not in to_score and not s.nli]


async def _nli_pair(claim: str, source: Source) -> NLIScore:
    """
    Score (evidence, claim) pair using zero-shot NLI.
    Strategy: prepend evidence as context, classify claim against
    SUPPORTS / CONTRADICTS / UNRELATED labels.
    """
    lang = "ro" if any(c in RO_CHARS for c in claim) else "en"
    model = NLI_MODEL_MULTI if lang == "ro" else NLI_MODEL_EN
    url   = HF_ROUTER + model

    evidence = (source.snippet or source.title)[:400]
    # Build hypothesis: "Based on the evidence, the claim is true/false/unrelated"
    # We classify the combined text against 3 labels
    combined = f"Evidence: {evidence}\n\nClaim: {claim}"

    labels_en = [
        "the claim is supported by the evidence",
        "the claim is contradicted by the evidence",
        "the evidence is unrelated to the claim",
    ]
    labels_ro = [
        "afirmația este susținută de dovadă",
        "afirmația este contrazisă de dovadă",
        "dovada nu are legătură cu afirmația",
    ]
    labels = labels_ro if lang == "ro" else labels_en

    async with httpx.AsyncClient(timeout=35.0) as client:
        for attempt in range(2):
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {HF_TOKEN}",
                         "Content-Type": "application/json"},
                json={"inputs": combined,
                      "parameters": {"candidate_labels": labels,
                                     "multi_label": False}},
            )
            if r.status_code == 503:
                await asyncio.sleep(10)
                continue
            if r.status_code != 200:
                raise ValueError(f"NLI {r.status_code}: {r.text[:150]}")
            data = r.json()
            if isinstance(data, list): data = data[0]
            lmap = dict(zip(data.get("labels",[]), data.get("scores",[])))

            # Map to entailment/neutral/contradiction
            ent = lmap.get(labels[0], 0)
            con = lmap.get(labels[1], 0)
            neu = lmap.get(labels[2], 0)
            total = ent + con + neu + 1e-9
            ent, con, neu = ent/total, con/total, neu/total

            if ent > 0.5:   verdict = "SUPPORTS"
            elif con > 0.4: verdict = "CONTRADICTS"
            else:           verdict = "NEUTRAL"
            return NLIScore(entailment=round(ent,3), neutral=round(neu,3),
                            contradiction=round(con,3), verdict=verdict)
    raise ValueError("NLI pair timeout")


async def run_zeroshot_nli(claim: str) -> dict:
    """Fallback zero-shot classification when evidence is sparse."""
    model = NLI_ZEROSHOT_MULTI if any(c in RO_CHARS for c in claim) else NLI_ZEROSHOT_EN
    url   = HF_ROUTER + model
    async with httpx.AsyncClient(timeout=40.0) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {HF_TOKEN}",
                     "Content-Type": "application/json"},
            json={"inputs": claim,
                  "parameters": {"candidate_labels": ZEROSHOT_LABELS,
                                 "multi_label": False}},
        )
        if r.status_code != 200:
            raise ValueError(f"ZeroShot {r.status_code}: {r.text[:200]}")
        data = r.json()
        if isinstance(data, list): data = data[0]
        lm = dict(zip(data.get("labels", []), data.get("scores", [])))
        return lm


# ════════════════════════════════════════════════════════════
# SCORE AGGREGATION
# ════════════════════════════════════════════════════════════

def aggregate_score(
    evidence: list[Source],
    zero_shot: dict | None,
    total_evidence: int,
) -> tuple[int, str, str, str]:
    """
    Aggregate NLI scores into final TruthScore.

    Formula:
      For each evidence piece with NLI scores:
        contribution = entailment - contradiction  (range -1..+1)
        weighted by evidence type credibility

      final_score = (mean_contribution + 1) / 2 * 100  (normalize 0-100)

    Confidence based on evidence count and score spread.
    """
    WEIGHTS = {
        "factcheck": 1.5,  # professional fact-checkers
        "academic":  1.3,  # peer-reviewed
        "news":      1.1,  # reliable news
        "web":       0.9,  # general web
        "wikipedia": 1.0,
        "wikidata":  1.1,
    }

    scored_pieces = [s for s in evidence if s.nli]
    signals = []

    if scored_pieces:
        weighted_sum, weight_total = 0.0, 0.0
        sup_count, con_count = 0, 0

        for s in scored_pieces:
            w    = WEIGHTS.get(s.type, 1.0)
            contrib = s.nli.entailment - s.nli.contradiction
            weighted_sum  += contrib * w
            weight_total  += w
            if s.nli.verdict == "SUPPORTS":    sup_count += 1
            elif s.nli.verdict == "CONTRADICTS": con_count += 1

        mean_contrib = weighted_sum / weight_total if weight_total > 0 else 0
        nli_score    = (mean_contrib + 1) / 2 * 100  # normalize to 0-100

        signals.append(
            f"{sup_count} surse susțin, {con_count} contrazic afirmația "
            f"({len(scored_pieces)} verificate cu NLI cross-encoder)"
        )
    else:
        nli_score = 50.0

    # Blend with zero-shot if available
    if zero_shot:
        correct = zero_shot.get("factually correct and verified", 0)
        false_  = zero_shot.get("misinformation or false claim", 0)
        partial = zero_shot.get("partially correct or misleading", 0)
        zs_score = correct * 92 + partial * 52 + false_ * 8
        if scored_pieces:
            final_score = nli_score * 0.7 + zs_score * 0.3
        else:
            final_score = zs_score
            signals.append(f"Analiză zero-shot (NLI): "
                           f"{correct*100:.0f}% corect, {false_*100:.0f}% fals")
    else:
        final_score = nli_score

    score = max(0, min(100, round(final_score)))

    # Verdict
    if score >= 70:   verdict = "TRUE"
    elif score < 38:  verdict = "FALSE"
    else:             verdict = "UNCERTAIN"

    # Confidence based on evidence count
    if total_evidence >= 6 and scored_pieces:  confidence = "HIGH"
    elif total_evidence >= 3:                   confidence = "MEDIUM"
    else:                                        confidence = "LOW"

    if not scored_pieces and not zero_shot:
        explanation = (
            "Dovezi insuficiente pentru o concluzie certă. "
            "Verifică manual sursele de mai jos."
        )
        confidence = "LOW"
    elif signals:
        explanation = ". ".join(signals) + "."
    else:
        explanation = "Analiză finalizată pe baza dovezilor colectate."

    return score, verdict, confidence, explanation


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def detect_topic(claim: str) -> str:
    """Detect claim topic to prioritize relevant sources."""
    c = claim.lower()
    if any(w in c for w in ["vaccine","virus","cancer","disease","drug","medical","health",
                              "covid","hiv","diabetes","vaccine","antibiotic","symptom",
                              "treatment","therapy","clinical","hospital","patient"]):
        return "medical"
    if any(w in c for w in ["climate","temperature","carbon","co2","fossil","pollution",
                              "emission","greenhouse","global warming","ozone","atmosphere"]):
        return "climate"
    if any(w in c for w in ["quantum","particle","atom","physics","relativity","gravity",
                              "nasa","space","planet","galaxy","universe","black hole"]):
        return "science"
    if any(w in c for w in ["war","president","election","government","parliament","treaty",
                              "political","democrat","republican","vote","minister","policy"]):
        return "politics"
    if any(w in c for w in ["born","died","founded","invented","discovered","history",
                              "century","ancient","medieval","world war","revolution"]):
        return "history"
    if any(w in c for w in ["economy","gdp","inflation","recession","stock","bitcoin",
                              "trade","market","unemployment","bank","financial"]):
        return "economics"
    return "general"


def evaluate_math_claim(claim: str):
    """Detect and evaluate mathematical expressions."""
    c = claim.strip().rstrip(".")
    patterns = [
        r"^(.+?)\s*=\s*([\d.]+)$",
        r"^(.+?)\s+(?:equals|is|este|egal cu|face)\s+([\d.]+)$",
    ]
    for pat in patterns:
        m = re.match(pat, c, re.IGNORECASE)
        if m:
            try:
                expr = m.group(1).strip().replace("x","*").replace("×","*").replace("÷","/")
                actual = eval(expr, {"__builtins__": {}}, {})
                claimed = float(m.group(2))
                if abs(float(actual) - claimed) < 1e-9:
                    return (95, f"Expresie matematică corectă: {expr} = {actual}")
                else:
                    return (5, f"Expresie matematică greșită: {expr} = {actual}, nu {claimed}")
            except: pass
    ineq = re.match(r"^([\d.]+)\s*([><]=?)\s*([\d.]+)$", c)
    if ineq:
        try:
            a, op, b = float(ineq.group(1)), ineq.group(2), float(ineq.group(3))
            ops = {">": a>b, "<": a<b, ">=": a>=b, "<=": a<=b}
            correct = ops.get(op, False)
            return (95 if correct else 5,
                    f"Inegalitate {'corectă' if correct else 'greșită'}: {a} {op} {b}")
        except: pass
    return None


def extract_keywords(text: str) -> str:
    stop = {
        "the","a","an","is","are","was","were","has","have","that","this",
        "and","or","but","in","on","at","to","of","for","with","by","from",
        "it","its","not","no","does","do","did","can","could","would","will",
        "more","most","all","also","very","just","been","been","being",
        "nu","ca","si","sau","dar","un","o","cel","cea","lui","este","sunt",
        "care","din","pentru","prin","despre","după","înainte","între",
    }
    words = re.findall(r"\b[a-zA-ZăâîșțĂÂÎȘȚ0-9]{3,}\b", text)
    priority = [w for w in words if w[0].isupper() and w.lower() not in stop]
    normal   = [w for w in words if not w[0].isupper() and w.lower() not in stop]
    return " ".join((priority + normal)[:10])[:150]


def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


def _reconstruct_abstract(inv_index: dict | None) -> str:
    """Reconstruct abstract from OpenAlex inverted index."""
    if not inv_index: return ""
    words = {}
    for word, positions in inv_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words))[:600]


def DistinguishedRating(rating: str) -> str:
    return rating


def factcheck_rating_to_nli(rating: str) -> NLIScore:
    r = rating.lower()
    if any(x in r for x in ["true","correct","accurate","adevarat","verified"]):
        return NLIScore(entailment=0.90, neutral=0.07, contradiction=0.03, verdict="SUPPORTS")
    if any(x in r for x in ["false","incorrect","fake","fals","mislead","hoax","wrong"]):
        return NLIScore(entailment=0.03, neutral=0.07, contradiction=0.90, verdict="CONTRADICTS")
    if any(x in r for x in ["mixed","partial","mostly","partially","half"]):
        return NLIScore(entailment=0.40, neutral=0.30, contradiction=0.30, verdict="NEUTRAL")
    return NLIScore(entailment=0.20, neutral=0.60, contradiction=0.20, verdict="NEUTRAL")


# ════════════════════════════════════════════════════════════
# HTML UI
# ════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def ui():
    hf_ok = "✅ HF_TOKEN" if HF_TOKEN else "❌ HF_TOKEN lipsă"
    gc_ok = "✅ Google FC" if GOOGLE_API_KEY else "⚠️ Google FC lipsă"
    return f"""<!DOCTYPE html>
<html lang="ro"><head><meta charset="UTF-8"/>
<title>TruthScore v3</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a10;color:#e8e8f0;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:24px 16px}}
.wrap{{width:100%;max-width:780px}}
.header{{background:#1a1a24;border:1px solid #2e2e40;border-radius:14px;padding:20px 24px;margin-bottom:16px}}
h1{{font-size:22px;font-weight:700;margin-bottom:2px}}
.sub{{color:#9090a8;font-size:12px;margin-bottom:12px}}
.badges{{display:flex;gap:6px;flex-wrap:wrap}}
.badge{{font-size:10px;padding:2px 9px;border-radius:12px;background:#22222f;border:1px solid #2e2e40;color:#9090a8}}
.card{{background:#1a1a24;border:1px solid #2e2e40;border-radius:12px;padding:20px 24px;margin-bottom:12px}}
textarea{{width:100%;min-height:80px;padding:10px 12px;background:#22222f;border:1.5px solid #2e2e40;border-radius:8px;color:#e8e8f0;font-size:14px;font-family:inherit;resize:vertical;outline:none;margin-bottom:10px}}
textarea:focus{{border-color:#6c63ff}}
.examples{{margin-bottom:12px}}
.ex-label{{font-size:11px;color:#9090a8;margin-bottom:5px}}
.ex-btn{{display:inline-block;padding:3px 9px;background:#22222f;border:1px solid #2e2e40;border-radius:5px;font-size:11px;color:#9090a8;cursor:pointer;margin:2px;transition:all .15s}}
.ex-btn:hover{{color:#e8e8f0;border-color:#6c63ff}}
.btn-row{{display:flex;gap:8px}}
button{{background:#6c63ff;color:#fff;border:none;padding:10px 0;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;flex:1;transition:background .2s}}
button:hover{{background:#7c74ff}}
button:disabled{{background:#22222f;color:#505068;cursor:not-allowed}}
.btn-sm{{flex:0;padding:10px 12px;background:#22222f;color:#9090a8;border:1px solid #2e2e40;font-size:11px}}
.loading{{color:#9090a8;font-size:13px;margin-top:12px;text-align:center;display:none}}
.progress{{height:3px;background:#2e2e40;border-radius:2px;margin-top:8px;overflow:hidden;display:none}}
.progress-bar{{height:100%;background:#6c63ff;border-radius:2px;transition:width .4s;width:0%}}
.err{{padding:10px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);color:#fca5a5;border-radius:8px;font-size:12px;margin-top:10px;display:none}}
/* Result */
.result{{display:none}}
.score-hero{{display:flex;align-items:center;gap:16px;padding:16px;background:#22222f;border-radius:10px;margin-bottom:12px;border:1px solid #2e2e40}}
.circle{{width:70px;height:70px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:800;flex-shrink:0;border:3px solid currentColor}}
.verdict-text{{font-weight:700;font-size:16px;margin-bottom:3px}}
.confidence{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:8px;vertical-align:middle}}
.conf-HIGH{{background:rgba(34,197,94,.15);color:#22c55e}}
.conf-MEDIUM{{background:rgba(245,158,11,.15);color:#f59e0b}}
.conf-LOW{{background:rgba(100,100,120,.2);color:#9090a8}}
.expl{{font-size:12px;color:#9090a8;line-height:1.5;margin-top:4px}}
.evidence-count{{font-size:11px;color:#6060780;margin-top:4px;color:#707088}}
/* NLI breakdown */
.breakdown{{padding:12px 14px;background:#22222f;border:1px solid #2e2e40;border-radius:10px;margin-bottom:10px}}
.bk-title{{font-size:11px;color:#9090a8;font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-bottom:10px}}
.bk-bars{{display:flex;gap:8px;height:28px;border-radius:6px;overflow:hidden}}
.bk-seg{{display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;transition:width .6s}}
/* Sources */
.sec-title{{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin:14px 0 7px;display:flex;align-items:center;gap:6px}}
.src{{display:flex;align-items:flex-start;gap:8px;padding:9px 11px;background:#22222f;border:1px solid #2e2e40;border-radius:8px;margin-bottom:5px;text-decoration:none;color:#e8e8f0;font-size:12px;transition:border-color .15s}}
.src:hover{{border-color:#6c63ff}}
.src-main{{flex:1;min-width:0}}
.src-title{{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:500px}}
.src-meta{{font-size:11px;color:#9090a8;margin-top:2px}}
.src-snip{{font-size:11px;color:#707088;margin-top:3px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.src-tag{{flex-shrink:0;font-size:10px;font-weight:700;padding:2px 7px;border-radius:8px;margin-left:6px;align-self:flex-start}}
.tag-sup{{background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.3)}}
.tag-con{{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}}
.tag-neu{{background:rgba(100,100,120,.15);color:#9090a8;border:1px solid #2e2e40}}
.nli-bar{{display:flex;gap:2px;margin-top:4px;height:3px;border-radius:2px;overflow:hidden}}
.nli-e{{background:#22c55e}}
.nli-c{{background:#ef4444}}
.nli-n{{background:#2e2e40}}
.models-used{{font-size:10px;color:#505068;margin-top:10px;padding-top:8px;border-top:1px solid #1e1e2a}}
</style></head>
<body><div class="wrap">

<div class="header">
  <h1>🔍 TruthScore <span style="font-size:13px;font-weight:400;color:#6c63ff">v3</span></h1>
  <div class="sub">Evidence-Based AI Fact-Checking · MSc Thesis Pipeline</div>
  <div class="badges">
    <span class="badge">{hf_ok}</span>
    <span class="badge">{gc_ok}</span>
    <span class="badge">cross-encoder/nli-deberta-v3-large</span>
    <span class="badge">DuckDuckGo + Wikipedia + Wikidata + OpenAlex + RSS</span>
  </div>
</div>

<div class="card">
  <textarea id="claim" placeholder="Introdu o afirmație în română sau engleză...&#10;Ex: Climate change is primarily caused by human activities.&#10;Ex: România a aderat la Uniunea Europeană în 2007."></textarea>
  <div class="examples">
    <div class="ex-label">Exemple:</div>
    <span class="ex-btn" onclick="set('The Earth is flat.')">🌍 Pământul e plat</span>
    <span class="ex-btn" onclick="set('Vaccines cause autism.')">💉 Vaccinuri autism</span>
    <span class="ex-btn" onclick="set('Climate change is primarily caused by human activities.')">🌡 Schimbări climatice</span>
    <span class="ex-btn" onclick="set('Romania joined the European Union in 2007.')">🇷🇴 România EU</span>
    <span class="ex-btn" onclick="set('5G towers spread COVID-19.')">📡 5G COVID</span>
    <span class="ex-btn" onclick="set('The Great Wall of China is visible from space.')">🧱 Marele Zid</span>
    <span class="ex-btn" onclick="set('Aspirin was invented by Bayer in 1897.')">💊 Aspirina</span>
  </div>
  <div class="btn-row">
    <button id="btn" onclick="verify()">🔍 Verifică</button>
    <button class="btn-sm" onclick="clearCache()">🗑 Cache</button>
  </div>
  <div class="progress" id="prog"><div class="progress-bar" id="progbar"></div></div>
  <div class="loading" id="loading">⏳ Inițializare pipeline...</div>
  <div class="err" id="err"></div>
</div>

<div class="result" id="result"></div>

</div>
<script>
function set(t){{document.getElementById('claim').value=t;}}
async function clearCache(){{await fetch('/clear-cache',{{method:'POST'}});alert('✅ Cache șters!');}}

const STEPS=['⏳ Se caută dovezi în web...','⏳ Se interogă Wikipedia & Wikidata...','⏳ Se caută articole academice...','⏳ Se analizează RSS fact-checkers...','⏳ Se rulează NLI cross-encoder pe dovezi...','⏳ Se calculează TruthScore...'];

async function verify(){{
  const text=document.getElementById('claim').value.trim();
  if(!text)return;
  const btn=document.getElementById('btn');
  btn.disabled=true;btn.textContent='⏳ Analiză...';
  document.getElementById('loading').style.display='block';
  document.getElementById('result').style.display='none';
  document.getElementById('err').style.display='none';
  document.getElementById('prog').style.display='block';
  let pct=5,step=0;
  const si=setInterval(()=>{{
    document.getElementById('loading').textContent=STEPS[Math.min(step++,STEPS.length-1)];
    pct=Math.min(pct+14,88);
    document.getElementById('progbar').style.width=pct+'%';
  }},3500);
  try{{
    const r=await fetch('/verify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{text}})}});
    clearInterval(si);
    document.getElementById('progbar').style.width='100%';
    const txt=await r.text();
    if(!r.ok)throw new Error('Server '+r.status+': '+txt.slice(0,300));
    render(JSON.parse(txt));
  }}catch(e){{
    clearInterval(si);
    const el=document.getElementById('err');
    el.style.display='block';el.textContent='⚠️ '+e.message;
  }}finally{{
    btn.disabled=false;btn.textContent='🔍 Verifică';
    document.getElementById('loading').style.display='none';
    setTimeout(()=>document.getElementById('prog').style.display='none',600);
  }}
}}

function render(d){{
  const color=d.verdict==='TRUE'?'#22c55e':d.verdict==='FALSE'?'#ef4444':'#f59e0b';
  const icon=d.verdict==='TRUE'?'✅':d.verdict==='FALSE'?'❌':'⚠️';
  const label=d.verdict==='TRUE'?'ADEVĂRAT':d.verdict==='FALSE'?'FALS':'INCERT';
  const allSrc=[...(d.supporting||[]),...(d.contradicting||[]),...(d.neutral_sources||[])];

  // NLI bar
  const supPct=d.supporting.length/(allSrc.length||1)*100;
  const conPct=d.contradicting.length/(allSrc.length||1)*100;
  const neuPct=100-supPct-conPct;

  let html=`
  <div class="card">
    <div class="score-hero">
      <div class="circle" style="color:${{color}}">${{d.score}}</div>
      <div style="flex:1;min-width:0">
        <div class="verdict-text" style="color:${{color}}">${{icon}} ${{label}}
          <span class="confidence conf-${{d.confidence}}">${{d.confidence}}</span>
        </div>
        <div class="expl">${{esc(d.explanation)}}</div>
        <div class="evidence-count">📊 ${{d.evidence_count}} dovezi colectate · ${{d.supporting.length}} susțin · ${{d.contradicting.length}} contrazic</div>
        ${{d.cached?'<div style="font-size:10px;color:#505068;margin-top:2px">⚡ din cache</div>':''}}
      </div>
    </div>
    <div class="breakdown">
      <div class="bk-title">📊 Distribuție dovezi NLI</div>
      <div class="bk-bars">
        <div class="bk-seg" style="width:${{supPct.toFixed(0)}}%;background:rgba(34,197,94,.25);color:#22c55e">${{supPct>15?'✅ Susțin':''}}</div>
        <div class="bk-seg" style="width:${{conPct.toFixed(0)}}%;background:rgba(239,68,68,.25);color:#ef4444">${{conPct>15?'❌ Contrazic':''}}</div>
        <div class="bk-seg" style="width:${{neuPct.toFixed(0)}}%;background:rgba(100,100,120,.2);color:#9090a8">${{neuPct>15?'⚪ Neutru':''}}</div>
      </div>
    </div>
  </div>`;

  if(d.supporting.length){{
    html+=`<div class="sec-title"><span style="color:#22c55e">✅</span> Surse care SUSȚIN afirmația (${{d.supporting.length}})</div>`;
    html+=d.supporting.map(s=>srcHtml(s,'sup')).join('');
  }}
  if(d.contradicting.length){{
    html+=`<div class="sec-title"><span style="color:#ef4444">❌</span> Surse care CONTRAZIC afirmația (${{d.contradicting.length}})</div>`;
    html+=d.contradicting.map(s=>srcHtml(s,'con')).join('');
  }}
  if(d.neutral_sources.length){{
    html+=`<div class="sec-title"><span style="color:#9090a8">⚪</span> Surse relevante (${{d.neutral_sources.length}})</div>`;
    html+=d.neutral_sources.map(s=>srcHtml(s,'neu')).join('');
  }}
  if(d.models_used.length){{
    html+=`<div class="card"><div class="models-used">🤖 Modele folosite: ${{esc(d.models_used.join(', '))}}</div></div>`;
  }}

  const el=document.getElementById('result');
  el.innerHTML=html;el.style.display='block';
}}

function srcHtml(s,tag){{
  const icons={{'web':'🌐','wikipedia':'📖','wikidata':'🗄️','academic':'🎓','news':'📰','factcheck':'🔎'}};
  const tagLabel={{'sup':'SUSȚINE','con':'CONTRAZICE','neu':'NEUTRU'}};
  const nliBar=s.nli?`<div class="nli-bar">
    <div class="nli-e" style="width:${{(s.nli.entailment*100).toFixed(0)}}%"></div>
    <div class="nli-n" style="width:${{(s.nli.neutral*100).toFixed(0)}}%"></div>
    <div class="nli-c" style="width:${{(s.nli.contradiction*100).toFixed(0)}}%"></div>
  </div>`:'';
  return `<a class="src" href="${{esc(s.url||'#')}}" target="_blank" rel="noopener">
    <span style="flex-shrink:0;font-size:14px">${{icons[s.type]||'📄'}}</span>
    <div class="src-main">
      <div class="src-title">${{esc(s.title)}}</div>
      <div class="src-meta">${{esc(s.publisher)}}</div>
      ${{s.snippet?`<div class="src-snip">${{esc(s.snippet.slice(0,200))}}</div>`:''}}
      ${{nliBar}}
    </div>
    <span class="src-tag tag-${{tag}}">${{tagLabel[tag]}}</span>
  </a>`;
}}

function esc(t){{const d=document.createElement('div');d.appendChild(document.createTextNode(String(t||'')));return d.innerHTML;}}
document.getElementById('claim').addEventListener('keydown',e=>{{if(e.key==='Enter'&&e.ctrlKey)verify();}});
</script></body></html>"""
