"""
TruthScore — Evidence retrieval (25+ sources).
"""
import logging
logger = logging.getLogger("truthscore.retrieval")
from config import *
from models import *
from pipeline.helpers import extract_keywords, detect_topic, _domain, _reconstruct_abstract, _strip_diacritics, factcheck_rating_to_nli, DistinguishedRating

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
    PubMed / NCBI -- 35 million biomedical abstracts.
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
    arXiv -- 2.3M preprints in physics, math, CS, biology, economics.
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
            pub     = ", ".join(authors) + " -- arXiv"
            if not title: continue
            sources.append(Source(
                type="academic", title=title[:120],
                url=url, snippet=summary,
                publisher=pub,
            ))
        return sources


async def search_semantic_scholar(claim: str) -> list[Source]:
    """
    Semantic Scholar -- 220M academic papers with AI-powered relevance.
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
            headers={"User-Agent": "TruthScore/3.0"},
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
                publisher=f"{authors} -- {venue}" if authors else venue,
            ))
        return sources


async def search_crossref(claim: str) -> list[Source]:
    """
    CrossRef -- 150M+ scholarly works DOI metadata.
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
                publisher=f"{author_str} -- {journal}" if author_str else journal,
            ))
        return sources


async def search_gdelt_news(claim: str) -> list[Source]:
    """
    GDELT DOC API -- searches 65+ countries, 100+ languages of news.
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
            # Format date YYYYMMDD -> YYYY-MM-DD
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
    CORE -- 200M open access research papers. Free API, no key needed for basic.
    https://core.ac.uk/
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get("https://api.core.ac.uk/v3/search/works", params={
            "q": kw, "limit": 5,
            "fields": "title,abstract,yearPublished,authors,sourceFulltextUrls,doi",
        }, headers={"User-Agent": "TruthScore/3.0"})
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
                publisher=f"{auth_str} -- CORE Open Access" if auth_str else "CORE Open Access",
            ))
        return sources


async def search_europe_pmc(claim: str) -> list[Source]:
    """
    Europe PMC -- 45M life sciences articles (PubMed + preprints + patents).
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
                publisher=f"{journal} -- Europe PMC",
            ))
        return sources


async def search_doaj(claim: str) -> list[Source]:
    """DOAJ -- 20M+ open access articles from peer-reviewed journals."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(
            "https://doaj.org/api/v3/search/articles",
            params={"q": kw, "pageSize": 5, "sort": "score"},
            headers={"User-Agent": "TruthScore/3.0"},
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
                publisher=f"{journal} (DOAJ - Peer Reviewed)",
            ))
        return sources


async def search_ieee(claim: str) -> list[Source]:
    """
    IEEE Xplore -- engineering, CS, electronics, AI papers.
    Requires free API key from: https://developer.ieee.org/
    Set IEEE_API_KEY in .env
    """
    ieee_key = os.getenv("IEEE_API_KEY", os.getenv("IEEE",""))
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
                publisher=f"{auth_str} -- {pub} (IEEE)" if auth_str else f"{pub} (IEEE)",
            ))
        return sources


async def search_scopus(claim: str) -> list[Source]:
    """
    Scopus (Elsevier) -- largest abstract/citation database.
    Requires institutional API key from: https://dev.elsevier.com/
    Set SCOPUS_API_KEY in .env (get it with your university account)
    """
    scopus_key = os.getenv("SCOPUS_API_KEY", os.getenv("SCOPUS",""))
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
                publisher=f"{creator} -- {journal} (Scopus)" if creator else f"{journal} (Scopus)",
            ))
        return sources


async def search_news_rss(claim: str) -> list[Source]:
    """
    Curated Tier-1 news RSS feeds only:
    Reuters, BBC, AP News, The Guardian -- all internationally recognized.
    """
    TIER1_FEEDS = [
        ("Reuters",      "https://feeds.reuters.com/reuters/topNews"),
        ("BBC News",     "https://feeds.bbci.co.uk/news/rss.xml"),
        ("AP News",      "https://apnews.com/rss"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
        ("FactCheck.org","https://www.factcheck.org/feed/"),
        ("Snopes",       "https://www.snopes.com/feed/"),
    ]
    keywords = set(w.lower() for w in extract_keywords(claim).split() if len(w) > 3)
    if not keywords: return []

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                                  headers={"User-Agent": "TruthScore/3.0"}) as client:
        responses = await asyncio.gather(
            *[client.get(url) for _, url in TIER1_FEEDS],
            return_exceptions=True
        )

    sources = []
    for (name, _), resp in zip(TIER1_FEEDS, responses):
        if isinstance(resp, Exception) or resp.status_code != 200: continue
        items = re.findall(r"<item[^>]*>(.*?)</item>", resp.text, re.DOTALL|re.IGNORECASE)
        for item in items[:30]:
            tm = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.DOTALL)
            lm = re.search(r"<link[^>]*>(https?://[^<]+)</link>|<guid[^>]*>(https?://[^<]+)</guid>", item)
            dm = re.search(r"<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", item, re.DOTALL)
            title   = (tm.group(1) if tm else "").strip()
            url     = (lm.group(1) or lm.group(2) if lm else "").strip()
            snippet = re.sub(r"<[^>]+>", "", dm.group(1) if dm else "").strip()[:250]
            if not title or len(title) < 5: continue
            # At least ONE keyword must match (case insensitive)
            title_lower   = title.lower()
            snippet_lower = snippet.lower()
            if not any(kw.lower() in title_lower or kw.lower() in snippet_lower for kw in keywords):
                continue
            sources.append(Source(type="news", title=title[:120], url=url,
                                  snippet=snippet, publisher=name))
            if len(sources) >= 5: break
        if len(sources) >= 5: break
    return sources[:4]


async def search_europeana(claim: str) -> list[Source]:
    """
    Europeana -- EU cultural heritage database. 50M+ artworks, artifacts,
    architectural records from European museums and archives. Free, no key.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://api.europeana.eu/record/v2/search.json",
            params={"query": kw, "rows": 5, "profile": "minimal",
                    "type": "IMAGE,TEXT", "reusability": "open"},
            headers={"User-Agent": "TruthScore/3.0"},
        )
        if r.status_code != 200: return []
        items = r.json().get("items", [])
        sources = []
        for item in items[:4]:
            title     = (item.get("title", [""])[0] or "")[:120]
            desc      = (item.get("dcDescription", [""])[0] or "")[:300] if item.get("dcDescription") else ""
            provider  = (item.get("dataProvider", ["Europeana"])[0] or "Europeana")
            item_url  = item.get("guid", "https://www.europeana.eu")
            if not title: continue
            sources.append(Source(
                type="academic", title=title,
                url=item_url, snippet=desc or f"Cultural heritage record from {provider}",
                publisher=f"{provider} -- Europeana",
            ))
        return sources


# ── OpenFDA -- US drug/device safety data (no key needed) ──────
async def search_openfda(claim: str) -> list[Source]:
    """FDA adverse events, drug labels, device reports. Highly credible."""
    kw = extract_keywords(claim)
    if not kw: return []
    sources = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Drug labels
        r = await client.get(
            "https://api.fda.gov/drug/label.json",
            params={"search": kw, "limit": 3},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r.status_code == 200:
            for item in r.json().get("results", [])[:3]:
                title = item.get("openfda", {}).get("brand_name", [""])[0] or "FDA Drug Label"
                desc  = " ".join(item.get("warnings", [""])[:1])[:300]
                sources.append(Source(
                    type="academic", title=title[:120],
                    url="https://open.fda.gov",
                    snippet=desc, publisher="US FDA OpenFDA"
                ))
        # Adverse events
        r2 = await client.get(
            "https://api.fda.gov/drug/event.json",
            params={"search": f"patient.drug.medicinalproduct:{kw.split()[0]}", "limit": 2},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r2.status_code == 200:
            total = r2.json().get("meta", {}).get("results", {}).get("total", 0)
            if total:
                sources.append(Source(
                    type="academic", title=f"FDA Adverse Events: {kw.split()[0]}",
                    url="https://open.fda.gov/apis/drug/event/",
                    snippet=f"{total:,} adverse event reports in FDA database",
                    publisher="US FDA OpenFDA"
                ))
    return sources[:3]


# ── World Bank -- economic & development data (no key) ──────────
async def search_world_bank(claim: str) -> list[Source]:
    """World Bank open data -- GDP, poverty, health, education indicators."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://search.worldbank.org/api/v2/wds",
            params={"q": kw, "rows": 4, "os": 0, "format": "json",
                    "fl": "docdt,display_title,url,abstracts,count"},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r.status_code != 200: return []
        docs = r.json().get("documents", {})
        sources = []
        for doc in list(docs.values())[:4]:
            if not isinstance(doc, dict): continue
            title   = doc.get("display_title", "")[:120]
            url     = doc.get("url", "https://data.worldbank.org")
            snippet = doc.get("abstracts", {}).get("cdata!", "")[:300]
            if not title: continue
            sources.append(Source(
                type="academic", title=title, url=url,
                snippet=snippet, publisher="World Bank"
            ))
        return sources[:3]


# ── IMF -- macroeconomic data (no key) ──────────────────────────
async def search_imf(claim: str) -> list[Source]:
    """IMF official publications and data."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://www.imf.org/external/datamapper/api/v1/",
            params={"q": kw},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r.status_code != 200: return []
        data = r.json()
        sources = []
        datasets = data.get("datasets", {})
        for k, v in list(datasets.items())[:3]:
            if not isinstance(v, dict): continue
            label = v.get("label", k)[:100]
            sources.append(Source(
                type="academic", title=f"IMF Data: {label}",
                url=f"https://www.imf.org/external/datamapper/{k}",
                snippet=v.get("description", f"IMF dataset: {label}")[:250],
                publisher="IMF -- International Monetary Fund"
            ))
        return sources[:2]


# ── OECD -- statistics and policy data (no key) ─────────────────
async def search_oecd(claim: str) -> list[Source]:
    """OECD stats -- education, health, economy, environment."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://stats.oecd.org/SDMX-JSON/data/",
            params={"q": kw, "format": "json"},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        # OECD SDMX is complex -- use search API instead
        r2 = await client.get(
            f"https://www.oecd.org/search/?q={kw}&lang=en&count=3",
            headers={"User-Agent": "TruthScore/4.0", "Accept": "application/json"}
        )
        sources = []
        if r2.status_code == 200:
            try:
                items = r2.json().get("results", [])
                for item in items[:3]:
                    sources.append(Source(
                        type="academic",
                        title=item.get("title", "OECD Publication")[:120],
                        url=item.get("url", "https://www.oecd.org"),
                        snippet=item.get("description", "")[:250],
                        publisher="OECD"
                    ))
            except Exception:
                # OECD doesn't return JSON for search -- use static link
                sources.append(Source(
                    type="academic",
                    title=f"OECD Statistics: {kw[:60]}",
                    url=f"https://stats.oecd.org/#searchRes={kw}",
                    snippet="OECD official statistics database",
                    publisher="OECD"
                ))
        return sources[:2]


# ── NOAA -- climate, weather, ocean data (no key) ───────────────
async def search_noaa(claim: str) -> list[Source]:
    """NOAA -- National Oceanic and Atmospheric Administration data."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://www.ncei.noaa.gov/cdo-web/api/v2/datasets",
            params={"datatypeid": kw.split()[0], "limit": 3},
            headers={"token": "anonymous", "User-Agent": "TruthScore/4.0"}
        )
        sources = []
        if r.status_code == 200:
            for ds in r.json().get("results", [])[:3]:
                sources.append(Source(
                    type="academic",
                    title=ds.get("name", "NOAA Dataset")[:120],
                    url="https://www.ncei.noaa.gov",
                    snippet=f"NOAA climate dataset. Period: {ds.get('mindate','?')} to {ds.get('maxdate','?')}",
                    publisher="NOAA -- National Oceanic and Atmospheric Administration"
                ))
        if not sources:
            # Fallback: NOAA news search
            r2 = await client.get(
                "https://www.noaa.gov/search/node/" + kw.replace(" ", "%20"),
                headers={"User-Agent": "TruthScore/4.0"}
            )
            if r2.status_code == 200:
                titles = re.findall(r'<h3[^>]*class="[^"]*title[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r2.text, re.DOTALL)
                for url, title in titles[:2]:
                    title = re.sub(r'<[^>]+>', '', title).strip()[:120]
                    if title:
                        sources.append(Source(
                            type="academic", title=title,
                            url=url if url.startswith("http") else "https://noaa.gov" + url,
                            snippet="NOAA official data and research",
                            publisher="NOAA"
                        ))
    return sources[:3]


# ── GDELT -- global news event database (no key) ────────────────
async def search_gdelt_events(claim: str) -> list[Source]:
    """GDELT -- monitors world's news in 100+ languages, real-time."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # GDELT requires specific query format
        r = await client.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": kw.replace('"', ''),  # GDELT doesn't support quotes
                "mode": "ArtList",
                "maxrecords": 4,
                "format": "json",
                "timespan": "2months",
                "sort": "DateDesc",
                "sourcelang": "english",
            },
            headers={"User-Agent": "Mozilla/5.0 TruthScore/4.0"}
        )
        if r.status_code != 200:
            print(f"  [GDELT] HTTP {r.status_code}")
            return []
        articles = r.json().get("articles", [])
        sources = []
        for art in articles[:4]:
            title  = art.get("title", "")[:120]
            url    = art.get("url", "")
            domain = art.get("domain", "")
            seendate = art.get("seendate", "")[:10]
            if not title or not url: continue
            sources.append(Source(
                type="news", title=title, url=url,
                snippet=f"GDELT news article from {domain} ({seendate})",
                publisher=domain or "GDELT Global News"
            ))
        return sources[:3]


# ── NewsAPI -- curated news from 80k+ sources (requires free key) ─
def _get_newsapi_domains(claim: str) -> str:
    """Return relevant news domains based on claim content."""
    c = claim.lower()
    sport_words = {"football","soccer","basketball","tennis","player","team","match",
                   "championship","league","fifa","nba","nfl","olympic","sport","athlete"}
    science_words = {"science","research","study","climate","health","medical","vaccine",
                     "nasa","space","physics","biology","chemistry"}
    if any(w in c for w in sport_words):
        return "bbc.co.uk,theguardian.com,apnews.com,reuters.com,espn.com,goal.com,skysports.com"
    elif any(w in c for w in science_words):
        return "reuters.com,bbc.co.uk,nature.com,sciencemag.org,newscientist.com,apnews.com"
    return "reuters.com,bbc.co.uk,apnews.com,theguardian.com,nytimes.com,washingtonpost.com"


async def search_newsapi(claim: str) -> list[Source]:
    """NewsAPI -- top headlines and everything from 80k+ sources."""
    if not NEWS_API_KEY: return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": kw, "pageSize": 5, "page": 1,
                "sortBy": "relevancy", "language": "en",
                "domains": _get_newsapi_domains(claim)
            },
            headers={"X-Api-Key": NEWS_API_KEY, "User-Agent": "TruthScore/4.0"}
        )
        if r.status_code != 200: return []
        sources = []
        for art in r.json().get("articles", [])[:4]:
            title  = art.get("title", "")[:120]
            url    = art.get("url", "")
            desc   = art.get("description", "")[:250]
            source = art.get("source", {}).get("name", "NewsAPI")
            if not title or not url or "[Removed]" in title: continue
            sources.append(Source(
                type="news", title=title, url=url,
                snippet=desc, publisher=source
            ))
        return sources[:3]



# ── The Guardian API -- quality journalism (free key) ──────────
async def search_guardian(claim: str) -> list[Source]:
    """The Guardian -- award-winning journalism, full article API."""
    if not GUARDIAN_API_KEY: return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Use named entity as exact phrase for better relevance
        named = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+", claim)
        search_q = f'"{named[0]}"' if named else kw

        # Detect sports context to restrict to sport section
        sport_words = {"football","soccer","basketball","tennis","athlete","player","team",
                       "match","championship","league","fifa","nba","nfl","olympic","sport"}
        is_sport = any(w in claim.lower() for w in sport_words)
        params = {
            "q": search_q, "api-key": GUARDIAN_API_KEY,
            "page-size": 5, "order-by": "relevance",
            "show-fields": "trailText,headline,shortUrl",
        }
        if is_sport:
            params["section"] = "sport"

        r = await client.get("https://content.guardianapis.com/search", params=params)
        if r.status_code != 200: return []
        sources = []
        for art in r.json().get("response", {}).get("results", [])[:4]:
            fields  = art.get("fields", {})
            title   = fields.get("headline") or art.get("webTitle", "")[:120]
            url     = art.get("webUrl", "")
            snippet = fields.get("trailText", "")[:280]
            if not title: continue
            sources.append(Source(
                type="news", title=title[:120], url=url,
                snippet=snippet, publisher="The Guardian"
            ))
        return sources[:3]


# ── NASA API -- space, earth, astronomy data (free key) ─────────
async def search_nasa(claim: str) -> list[Source]:
    """NASA open data -- astronomy, earth science, space exploration."""
    kw = extract_keywords(claim)
    if not kw: return []
    sources = []
    async with httpx.AsyncClient(timeout=12.0) as client:
        # NASA Technical Reports Server (no key needed)
        r = await client.get(
            "https://ntrs.nasa.gov/api/citations/search",
            params={"q": kw, "rows": 4},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r.status_code == 200:
            for item in r.json().get("results", [])[:3]:
                title   = item.get("title", "")[:120]
                url     = f"https://ntrs.nasa.gov/citations/{item.get('id','')}"
                snippet = item.get("abstract", "")[:280]
                if title:
                    sources.append(Source(
                        type="academic", title=title, url=url,
                        snippet=snippet, publisher="NASA Technical Reports"
                    ))

        # NASA Image and Video Library
        if NASA_API_KEY:
            r2 = await client.get(
                "https://api.nasa.gov/planetary/apod",
                params={"api_key": NASA_API_KEY, "count": 2},
            )
            if r2.status_code == 200:
                for item in r2.json()[:2]:
                    title   = item.get("title", "")[:120]
                    snippet = item.get("explanation", "")[:280]
                    url     = item.get("url", "https://apod.nasa.gov")
                    if title:
                        sources.append(Source(
                            type="academic", title=title, url=url,
                            snippet=snippet, publisher="NASA APOD"
                        ))
    return sources[:4]


# ── OpenFDA with API key ───────────────────────────────────────
# (overrides the earlier version, uses the key for higher rate limits)
async def search_openfda_v2(claim: str) -> list[Source]:
    """OpenFDA with API key -- higher rate limit (240 req/min)."""
    kw = extract_keywords(claim)
    if not kw: return []
    params_extra = {}
    if OPENFDA_API_KEY:
        params_extra["api_key"] = OPENFDA_API_KEY

    sources = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Drug label search
        r = await client.get(
            "https://api.fda.gov/drug/label.json",
            params={"search": kw, "limit": 3, **params_extra},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r.status_code == 200:
            for item in r.json().get("results", [])[:3]:
                brand  = item.get("openfda", {}).get("brand_name", [""])[0]
                generic = item.get("openfda", {}).get("generic_name", [""])[0]
                title  = f"FDA Drug Label: {brand or generic or kw}"[:120]
                warnings = item.get("warnings", [""])[0][:280] if item.get("warnings") else ""
                indications = item.get("indications_and_usage", [""])[0][:280] if item.get("indications_and_usage") else ""
                snippet = warnings or indications or "FDA official drug label"
                sources.append(Source(
                    type="academic", title=title,
                    url="https://open.fda.gov/drugs/",
                    snippet=snippet[:280], publisher="US FDA -- OpenFDA"
                ))

        # Adverse events count
        r2 = await client.get(
            "https://api.fda.gov/drug/event.json",
            params={"search": f"patient.drug.openfda.generic_name:{kw.split()[0]}",
                    "count": "serious", "limit": 1, **params_extra},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r2.status_code == 200:
            total = r2.json().get("meta", {}).get("results", {}).get("total", 0)
            if total > 0:
                sources.append(Source(
                    type="academic",
                    title=f"FDA Adverse Events Database: {kw.split()[0]}",
                    url="https://open.fda.gov/apis/drug/event/",
                    snippet=f"{total:,} adverse event reports filed with FDA for this substance.",
                    publisher="US FDA -- OpenFDA"
                ))
    return sources[:3]


# ── NOAA with token -- climate & weather data ───────────────────
async def search_noaa_v2(claim: str) -> list[Source]:
    """NOAA with authentication token -- higher rate limits."""
    kw = extract_keywords(claim)
    if not kw: return []
    token = NOAA_TOKEN or "anonymous"
    sources = []
    async with httpx.AsyncClient(timeout=12.0) as client:
        # NOAA CDO datasets
        r = await client.get(
            "https://www.ncei.noaa.gov/cdo-web/api/v2/datasets",
            params={"limit": 4, "keywords": kw.split()[0]},
            headers={"token": token, "User-Agent": "TruthScore/4.0"}
        )
        if r.status_code == 200:
            for ds in r.json().get("results", [])[:3]:
                sources.append(Source(
                    type="academic",
                    title=ds.get("name", "NOAA Dataset")[:120],
                    url="https://www.ncei.noaa.gov",
                    snippet=(f"NOAA climate data. Period: {ds.get('mindate','?')} to "
                             f"{ds.get('maxdate','?')}. {ds.get('datacoverage','')}"),
                    publisher="NOAA -- National Oceanic and Atmospheric Administration"
                ))

        # NOAA climate.gov search
        r2 = await client.get(
            "https://www.climate.gov/news-features/search/node/" + kw.replace(" ", "%20"),
            headers={"User-Agent": "TruthScore/4.0"},
        )
        if r2.status_code == 200:
            titles = re.findall(
                r'<span[^>]*class="[^"]*field--name-title[^"]*"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                r2.text, re.DOTALL
            )
            for url, title in titles[:2]:
                title = re.sub(r'<[^>]+>', '', title).strip()[:120]
                if title:
                    full_url = url if url.startswith("http") else "https://www.climate.gov" + url
                    sources.append(Source(
                        type="academic", title=title, url=full_url,
                        snippet="NOAA Climate.gov official article",
                        publisher="NOAA Climate.gov"
                    ))
    return sources[:4]


async def search_ddg_wiki(claim: str) -> list[Source]:
    """
    Broad DuckDuckGo search across ALL trusted sources -- NOT Wikipedia-only.
    Wikipedia is included but NOT prioritised over other authoritative sources.
    Trusted domains: encyclopedic, academic, news, government, fact-check.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        loop = asyncio.get_event_loop()

        # High-credibility domains -- Wikipedia excluded, Britannica preferred
        PRIORITY_DOMAINS = [
            "britannica.com",                    # encyclopaedic, peer-reviewed
            "scholarpedia.org",                  # expert-written
            "pubmed.ncbi.nlm.nih.gov",           # biomedical
            "ncbi.nlm.nih.gov",                  # biomedical
            "who.int", "cdc.gov", "nih.gov",     # health agencies
            "nature.com", "science.org",         # peer-reviewed science
            "reuters.com", "bbc.com", "apnews.com",  # tier-1 news
            "snopes.com", "factcheck.org", "politifact.com",  # fact-checkers
            "wolframalpha.com",                  # computational facts
            "ourworldindata.org",                # data-driven
            "scholar.google.com",                # academic
            # Wikipedia intentionally excluded from DDG search priority
        ]

        def _search():
            with DDGS() as ddgs:
                queries = []

                # Strategy 1: direct factual query (no site: restriction)
                queries.append(f"{claim[:120]} facts")

                # Strategy 2: named entity + fact check
                caps = re.findall(r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*", claim)
                if caps:
                    entity = caps[0]
                    queries.append(f"{entity} {claim[:60]}")

                # Strategy 3: claim + verify
                queries.append(f"is it true that {claim[:100]}")

                all_results = []
                seen_urls = set()
                for q in queries[:3]:
                    results = list(ddgs.text(q, max_results=10))
                    for r in results:
                        url = r.get("href", "")
                        if url not in seen_urls:
                            seen_urls.add(url)
                            all_results.append(r)
                    if len(all_results) >= 15:
                        break
                return all_results

        hits = await asyncio.wait_for(
            loop.run_in_executor(None, _search), timeout=15.0
        )

        # Sort: priority domains first, then others
        def _priority(h):
            url = h.get("href", "")
            for i, d in enumerate(PRIORITY_DOMAINS):
                if d in url:
                    return i
            return len(PRIORITY_DOMAINS)

        hits_sorted = sorted(hits, key=_priority)

        sources = []
        for h in hits_sorted[:10]:
            url   = h.get("href", "")
            title = h.get("title", "")[:120]
            body  = h.get("body", "")[:400]
            if not title or not body:
                continue
            if _is_low_quality(url, body):
                continue

            domain = _domain(url)
            if "wikipedia.org" in url:
                src_type = "wikipedia"
            elif any(d in url for d in ["pubmed", "ncbi", "nature.com", "arxiv"]):
                src_type = "academic"
            elif any(d in url for d in ["snopes", "factcheck", "politifact"]):
                src_type = "factcheck"
            elif any(d in url for d in ["reuters", "bbc", "apnews", "guardian"]):
                src_type = "news"
            else:
                src_type = "web"

            sources.append(Source(
                type=src_type,
                title=title,
                url=url,
                snippet=body,
                publisher=domain,
            ))

        print(f"  [DDG-BROAD] {len(sources)} results | types: "
              f"{[s.type for s in sources[:5]]}")
        return sources[:6]

    except Exception as e:
        print(f"  [DDG-BROAD] error: {e}")
        return []



async def search_tavily(claim: str) -> list[Source]:
    """
    Tavily Search -- purpose-built for AI research and fact-checking.
    COST-CONTROLLED version:
      1. Evidence cache first (free, 24h TTL) — repeated queries cost $0
      2. Global daily budget guard — protects against runaway spend
      3. "basic" search depth (1 credit) instead of "advanced" (2 credits)
         -> halves cost per call; quality loss is minimal because we already
            restrict to trusted domains.
    Free tier: 1000 searches/month from https://tavily.com ($0.008/credit after)
    Set TAVILY_API_KEY in .env
    """
    if not TAVILY_API_KEY:
        return []
    kw = claim[:200]

    # ── Layer 1: evidence cache (FREE) ─────────────────────────
    try:
        from utils.evidence_cache import get_cached_evidence, store_cached_evidence
        cached = await get_cached_evidence(kw, "tavily")
        if cached is not None:
            print(f"  [TAVILY] CACHE HIT ({len(cached)} sources) — $0 spent")
            return [Source(**s) for s in cached]
    except Exception as e:
        logger.debug("evidence cache read failed: %s", e)

    # ── Layer 2: daily budget guard ────────────────────────────
    try:
        from utils.evidence_cache import paid_search_allowed
        if not await paid_search_allowed("tavily"):
            print(f"  [TAVILY] BUDGET EXHAUSTED — skipping paid search")
            return []
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key":               TAVILY_API_KEY,
                    "query":                 kw,
                    "search_depth":          os.getenv("TAVILY_DEPTH", "basic"),  # basic=1 credit (was: advanced=2)
                    "include_answer":        True,         # get AI summary
                    "include_raw_content":   False,
                    "max_results":           7,
                    "include_domains": [                   # trusted only
                        "britannica.com", "pubmed.ncbi.nlm.nih.gov",
                        "reuters.com", "bbc.com", "apnews.com",
                        "who.int", "cdc.gov", "nih.gov",
                        "nature.com", "science.org",
                        "snopes.com", "factcheck.org", "politifact.com",
                        "nasa.gov", "britannica.com",
                        "sciencedirect.com", "scholar.google.com",
                    ],
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code != 200:
                print(f"  [TAVILY] {r.status_code}: {r.text[:80]}")
                return []
            data = r.json()
            sources = []

            # Tavily answer (AI summary from web)
            answer = data.get("answer", "")
            if answer and len(answer) > 20:
                sources.append(Source(
                    type="factcheck",
                    title=f"Tavily Research: {claim[:60]}",
                    url="https://tavily.com",
                    snippet=answer[:500],
                    publisher="Tavily AI Research",
                ))

            # Individual results
            for item in data.get("results", [])[:6]:
                title   = (item.get("title") or "")[:120]
                url     = item.get("url", "")
                content_snippet = (item.get("content") or "")[:400]
                domain  = _domain(url)
                if not title or not url:
                    continue
                # Determine source type
                if any(d in url for d in ["pubmed", "ncbi", "nature", "sciencedirect"]):
                    src_type = "academic"
                elif any(d in url for d in ["snopes", "factcheck", "politifact", "fullfact"]):
                    src_type = "factcheck"
                elif any(d in url for d in ["reuters", "bbc", "apnews", "guardian"]):
                    src_type = "news"
                elif "britannica" in url:
                    src_type = "academic"
                else:
                    src_type = "web"
                sources.append(Source(
                    type=src_type,
                    title=title,
                    url=url,
                    snippet=content_snippet,
                    publisher=domain,
                ))
            print(f"  [TAVILY] {len(sources)} results (incl. AI summary)")
            result = sources[:7]
            # Store in evidence cache — future identical queries cost $0
            try:
                from utils.evidence_cache import store_cached_evidence
                await store_cached_evidence(kw, "tavily", [s.model_dump() for s in result])
            except Exception as e:
                logger.debug("evidence cache store failed: %s", e)
            return result
        except Exception as e:
            print(f"  [TAVILY] error: {e}")
            return []


async def search_britannica(claim: str) -> list[Source]:
    """
    Encyclopaedia Britannica -- expert-authored, fact-checked encyclopaedia.
    More reliable than Wikipedia for fact-checking.
    Uses DDG restricted to britannica.com only.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        loop = asyncio.get_event_loop()
        kw   = extract_keywords(claim)[:80]

        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(
                    f"site:britannica.com {kw}",
                    max_results=5
                ))

        hits = await asyncio.wait_for(
            loop.run_in_executor(None, _search), timeout=10.0
        )
        sources = []
        for h in hits[:4]:
            url   = h.get("href", "")
            title = h.get("title", "")[:120]
            body  = h.get("body", "")[:400]
            if not title or not body:
                continue
            sources.append(Source(
                type="academic",
                title=title,
                url=url,
                snippet=body,
                publisher="Encyclopaedia Britannica",
            ))
        print(f"  [BRITANNICA] {len(sources)} results")
        return sources[:3]
    except Exception as e:
        print(f"  [BRITANNICA] error: {e}")
        return []


async def search_wikipedia(claim: str) -> list[Source]:
    """
    Wikipedia via multiple unrestricted endpoints:
    1. Wikipedia Summary API (en.wikipedia.org/api/rest_v1/page/summary)
    2. Wikipedia OpenSearch (autocomplete, no auth needed)
    3. DuckDuckGo Instant Answer (pulls from Wikipedia)
    """
    if not claim.strip(): return []
    sources = []
    seen = set()
    words = claim.split()
    # Build candidate page titles from query
    candidates = [
        claim.strip(),
        " ".join(words[:4]),
        " ".join(words[:2]),
    ]
    if len(words) > 0:
        candidates.append(words[0])

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
        headers={"User-Agent": "TruthScore/4.0 (educational research bot)"}) as client:

        # Strategy 1: Wikipedia Summary API (different from search, usually not blocked)
        for title_guess in candidates[:4]:
            if len(sources) >= 3: break
            title_slug = title_guess.replace(" ", "_")
            try:
                r = await client.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{title_slug}",
                )
                print(f"  [WIKI] summary/{title_slug!r} -> {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    title   = d.get("title","")
                    snippet = d.get("extract","")[:400]
                    url     = d.get("content_urls",{}).get("desktop",{}).get("page","")
                    if title and title not in seen:
                        seen.add(title)
                        sources.append(Source(type="wikipedia", title=title,
                                              url=url or f"https://en.wikipedia.org/wiki/{title_slug}",
                                              snippet=snippet, publisher="Wikipedia (EN)"))
            except Exception as e:
                print(f"  [WIKI] summary error: {e}")

        # Strategy 2: Wikipedia OpenSearch (autocomplete) - rarely blocked
        if len(sources) < 2:
            for q in [claim.strip(), " ".join(words[:3])]:
                try:
                    r = await client.get(
                        "https://en.wikipedia.org/w/api.php",
                        params={"action":"opensearch","search":q,"limit":5,
                                "namespace":0,"format":"json"},
                    )
                    print(f"  [WIKI] opensearch {q!r} -> {r.status_code}")
                    if r.status_code == 200:
                        data = r.json()
                        titles = data[1] if len(data)>1 else []
                        descs  = data[2] if len(data)>2 else []
                        urls   = data[3] if len(data)>3 else []
                        for t, d2, u in zip(titles[:4], descs[:4], urls[:4]):
                            if t and t not in seen:
                                seen.add(t)
                                sources.append(Source(type="wikipedia", title=t,
                                                      url=u, snippet=d2[:300],
                                                      publisher="Wikipedia (EN)"))
                        if sources: break
                except Exception as e:
                    print(f"  [WIKI] opensearch error: {e}")

        # Strategy 3: DuckDuckGo Instant Answer (pulls Wikipedia data, no blocks)
        if not sources:
            try:
                r = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": claim, "format": "json",
                            "no_html": "1", "skip_disambig": "1"},
                )
                print(f"  [WIKI] ddg {claim[:40]!r} -> {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    abstract = d.get("Abstract","")
                    title    = d.get("Heading","")
                    url      = d.get("AbstractURL","")
                    if abstract and title and title not in seen:
                        seen.add(title)
                        sources.append(Source(type="wikipedia", title=title,
                                              url=url, snippet=abstract[:400],
                                              publisher="Wikipedia via DuckDuckGo"))
                    # Also check RelatedTopics
                    for rt in d.get("RelatedTopics",[])[:3]:
                        if isinstance(rt, dict) and rt.get("Text"):
                            rt_title = rt.get("Text","")[:80]
                            if rt_title not in seen:
                                seen.add(rt_title)
                                sources.append(Source(type="wikipedia",
                                                      title=rt_title,
                                                      url=rt.get("FirstURL",""),
                                                      snippet=rt.get("Text","")[:300],
                                                      publisher="DuckDuckGo"))
            except Exception as e:
                print(f"  [WIKI] ddg error: {e}")

    return sources[:4]


# ── GeoNames API -- 11M geographic locations (free, register at geonames.org) ──
# ── PubChem -- chemical compounds database (free, no key) ──────────────────
async def search_pubchem(claim: str) -> list[Source]:
    """PubChem -- US National Library of Medicine chemical database.
    120M+ compounds, bioactivities, safety data. Completely free."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # PubChem full-text search
        r = await client.get(
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/" +
            kw.split()[0] + "/JSON",
            headers={"User-Agent": "TruthScore/4.0"}
        )
        sources = []
        if r.status_code == 200:
            compounds = r.json().get("PC_Compounds", [])
            for c in compounds[:3]:
                cid = c.get("id",{}).get("id",{}).get("cid","")
                props = {p["urn"]["label"]: p.get("value",{})
                         for p in c.get("props",[]) if "label" in p.get("urn",{})}
                name    = (props.get("IUPAC Name",{}).get("sval","") or
                           props.get("Preferred",{}).get("sval","") or kw.split()[0])
                formula = props.get("Molecular Formula",{}).get("sval","")
                weight  = props.get("Molecular Weight",{}).get("fval","")
                snippet = f"Chemical formula: {formula}" if formula else ""
                if weight: snippet += f", Molecular weight: {weight}"
                if cid:
                    sources.append(Source(
                        type="academic",
                        title=f"PubChem: {name[:80]}",
                        url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                        snippet=snippet or f"Chemical compound record: {name}",
                        publisher="PubChem -- NCBI"
                    ))
        # Also search PubChem full-text
        r2 = await client.get(
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/" +
            kw.replace(" ","+") + "/cids/JSON?name_type=word",
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r2.status_code == 200 and not sources:
            cids = r2.json().get("IdentifierList",{}).get("CID",[])
            for cid in cids[:2]:
                sources.append(Source(
                    type="academic",
                    title=f"PubChem Compound CID {cid}",
                    url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    snippet=f"Chemical compound database entry for: {kw}",
                    publisher="PubChem -- NCBI"
                ))
    print(f"  [PUBCHEM] {len(sources)} results")
    return sources[:3]


# ── Open Library -- books, authors, literature (free, no key) ───────────────
async def search_open_library(claim: str) -> list[Source]:
    """Open Library -- 20M+ books, authors, works. Part of Internet Archive."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://openlibrary.org/search.json",
            params={"q": kw, "limit": 5, "fields": "title,author_name,first_publish_year,subject"},
            headers={"User-Agent": "TruthScore/4.0"}
        )
        if r.status_code != 200: return []
        sources = []
        for doc in r.json().get("docs", [])[:4]:
            title   = doc.get("title","")[:120]
            authors = ", ".join(doc.get("author_name",[])[:2])
            year    = doc.get("first_publish_year","")
            subjects = ", ".join(doc.get("subject",[])[:3])
            if not title: continue
            snippet = f"By {authors}" if authors else ""
            if year:     snippet += f" ({year})"
            if subjects: snippet += f". Subjects: {subjects}"
            key = doc.get("key","")
            sources.append(Source(
                type="academic",
                title=title,
                url=f"https://openlibrary.org{key}" if key else "https://openlibrary.org",
                snippet=snippet[:300],
                publisher="Open Library (Internet Archive)"
            ))
        print(f"  [OPENLIBRARY] {len(sources)} results")
        return sources[:3]



# ══════════════════════════════════════════════════════════════
# NEW CURATED APIs -- Art, Culture, Geography, Science
# ══════════════════════════════════════════════════════════════

# ── Metropolitan Museum of Art -- free, no key ─────────────────
async def search_met_museum(claim: str) -> list[Source]:
    """Metropolitan Museum of Art -- 470k+ open access artworks."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://collectionapi.metmuseum.org/public/collection/v1/search",
            params={"q": kw, "hasImages": True, "limit": 5})
        if r.status_code != 200: return []
        ids = r.json().get("objectIDs", [])[:4]
        if not ids: return []
        sources = []
        for oid in ids[:3]:
            r2 = await client.get(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}")
            if r2.status_code != 200: continue
            d = r2.json()
            title  = d.get("title","")[:120]
            artist = d.get("artistDisplayName","")
            date   = d.get("objectDate","")
            medium = d.get("medium","")
            dept   = d.get("department","")
            url    = d.get("objectURL","https://www.metmuseum.org")
            if not title: continue
            snippet = f"By {artist}" if artist else ""
            if date:   snippet += f", {date}"
            if medium: snippet += f". Medium: {medium}"
            if dept:   snippet += f". Department: {dept}"
            sources.append(Source(type="academic", title=title, url=url,
                snippet=snippet[:300], publisher="The Metropolitan Museum of Art"))
        print(f"  [MET] {len(sources)} artworks")
        return sources[:3]


# ── Smithsonian Institution -- free, key optional ───────────────
async def search_smithsonian(claim: str) -> list[Source]:
    """Smithsonian Open Access -- 4.4M objects from 19 museums. No key needed."""
    kw = extract_keywords(claim)
    if not kw: return []
    # API key is optional - works without it, just lower rate limit
    key = os.getenv("SMITHSONIAN_API_KEY", "e6v9WNQE2vmGVvWm8I1ZOiH8f1BVNcFXbYxpvQpW")  # default demo key
    params = {"q": kw, "rows": 5, "start": 0, "api_key": key}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.si.edu/openaccess/api/v1.0/search", params=params,
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code != 200: return []
        rows = r.json().get("response",{}).get("rows",[])
        sources = []
        for row in rows[:4]:
            d      = row.get("content",{}).get("descriptiveNonRepeating",{})
            title  = (row.get("title","") or d.get("title",{}).get("content",""))[:120]
            url    = (d.get("record_ID","") or row.get("id",""))
            if url and not url.startswith("http"):
                url = f"https://collections.si.edu/search/detail/{url}"
            notes  = row.get("content",{}).get("freetext",{})
            desc   = " ".join(n.get("content","") for n in notes.get("notes",notes.get("physicalDescription",notes.get("topic",[])))[:2])[:280]
            if not title: continue
            sources.append(Source(type="academic", title=title, url=url or "https://si.edu",
                snippet=desc or "Smithsonian collection record", publisher="Smithsonian Institution"))
        print(f"  [SMITHSONIAN] {len(sources)} results")
        return sources[:3]


# ── OpenStreetMap / Nominatim -- geographic search, free ────────
async def search_nominatim(claim: str) -> list[Source]:
    """OpenStreetMap Nominatim -- geographic lookup for places, landmarks, structures."""
    words = [w for w in claim.split() if len(w) > 2]
    if not words: return []
    caps = [w for w in words if w[0].isupper()]
    query = " ".join(caps[:4]) if caps else " ".join(words[:3])
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 4,
                    "addressdetails": 1, "extratags": 1},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code != 200: return []
        sources = []
        for item in r.json()[:4]:
            name    = item.get("display_name","")[:120]
            osm_type = item.get("type","")
            osm_class = item.get("class","")
            lat     = item.get("lat","")
            lon     = item.get("lon","")
            importance = item.get("importance",0)
            addr    = item.get("address",{})
            country = addr.get("country","")
            tags    = item.get("extratags",{})
            wiki    = tags.get("wikipedia","")
            url     = f"https://www.openstreetmap.org/{item.get('osm_type','node')}/{item.get('osm_id','')}"
            snippet = f"Type: {osm_class}/{osm_type}"
            if country: snippet += f", Country: {country}"
            if lat: snippet += f", Coordinates: {lat}°N {lon}°E"
            if wiki: snippet += f", Wikipedia: {wiki}"
            if not name: continue
            sources.append(Source(type="academic", title=name[:120], url=url,
                snippet=snippet[:300], publisher="OpenStreetMap / Nominatim"))
        print(f"  [OSM] {len(sources)} results for {query!r}")
        return sources[:3]


# ── UNESCO World Heritage Sites -- free, no key ─────────────────
async def search_unesco(claim: str) -> list[Source]:
    """UNESCO World Heritage Sites database -- 1200+ sites worldwide."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://whc.unesco.org/api/sites/",
            params={"search": kw, "fmt": "json", "order": "name",
                    "category": "Mixed,Cultural,Natural", "number": 5},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code != 200:
            # Fallback: UNESCO search page
            r = await client.get("https://whc.unesco.org/en/list/",
                params={"search": kw.split()[0], "order": "name"})
            if r.status_code != 200: return []
            titles = re.findall(r'<a[^>]+href="/en/list/(\d+)"[^>]*>(.*?)</a>', r.text)
            sources = []
            for site_id, title in titles[:3]:
                title = re.sub(r"<[^>]+>","",title).strip()[:120]
                if title:
                    sources.append(Source(type="academic",
                        title=f"UNESCO World Heritage: {title}",
                        url=f"https://whc.unesco.org/en/list/{site_id}/",
                        snippet="UNESCO World Heritage Site",
                        publisher="UNESCO World Heritage"))
            print(f"  [UNESCO] {len(sources)} results (fallback)")
            return sources[:3]
        try:
            data = r.json()
            sites = data if isinstance(data, list) else data.get("sites", data.get("results",[]))
        except Exception:
            return []
        sources = []
        for s in (sites or [])[:4]:
            title   = s.get("site","") or s.get("name","") or s.get("full_name","")
            site_id = s.get("id_number","") or s.get("id","")
            desc    = s.get("short_description","") or s.get("justification","")
            country = s.get("states_name_en","") or s.get("country","")
            if not title: continue
            snippet = f"Country: {country}" if country else ""
            if desc: snippet += f". {desc[:200]}"
            sources.append(Source(type="academic",
                title=f"UNESCO: {title[:100]}",
                url=f"https://whc.unesco.org/en/list/{site_id}/",
                snippet=snippet[:300], publisher="UNESCO World Heritage"))
        print(f"  [UNESCO] {len(sources)} heritage sites")
        return sources[:3]


# ── Harvard Art Museums -- free with key ────────────────────────
async def search_harvard_art(claim: str) -> list[Source]:
    """Harvard Art Museums -- 250k+ artworks, scholarly metadata."""
    key = os.getenv("HARVARD_API_KEY", os.getenv("HARVARD", ""))
    if not key: return []  # requires free key from api.harvardartmuseums.org
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.harvardartmuseums.org/object",
            params={"apikey": key, "keyword": kw, "size": 4,
                    "fields": "title,dated,description,url,division,technique,artistdisplaydates"})
        if r.status_code != 200: return []
        sources = []
        for obj in r.json().get("records",[])[:4]:
            title = obj.get("title","")[:120]
            desc  = obj.get("description","")[:280]
            dated = obj.get("dated","")
            div   = obj.get("division","")
            url   = obj.get("url","https://harvardartmuseums.org")
            if not title: continue
            snippet = f"{div}, {dated}" if div else dated
            if desc: snippet += f". {desc}"
            sources.append(Source(type="academic", title=title, url=url,
                snippet=snippet[:300], publisher="Harvard Art Museums"))
        return sources[:3]


# ── National Park Service -- US parks, history, nature ──────────
async def search_nps(claim: str) -> list[Source]:
    """National Park Service API -- US national parks, monuments, historic sites."""
    key = os.getenv("NPS_API_KEY", os.getenv("NPS", ""))
    if not key: return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://developer.nps.gov/api/v1/parks",
            params={"q": kw, "api_key": key, "limit": 5,
                    "fields": "fullName,description,latLong,topics,activities"})
        if r.status_code != 200: return []
        sources = []
        for park in r.json().get("data",[])[:4]:
            title = park.get("fullName","")[:120]
            desc  = park.get("description","")[:300]
            url   = park.get("url","https://www.nps.gov")
            if not title: continue
            sources.append(Source(type="academic", title=title, url=url,
                snippet=desc, publisher="US National Park Service"))
        return sources[:3]


# ── Historic England -- listed buildings, heritage data ─────────
async def search_historic_england(claim: str) -> list[Source]:
    """Historic England -- National Heritage List, 400k+ listed buildings."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://historicengland.org.uk/images-books/archive/collections/aerial-photos/results/",
            params={"search": kw, "format": "json"},
            headers={"User-Agent": "TruthScore/4.0"})
        # Historic England doesn't have a simple open API -- use search fallback
        r2 = await client.get("https://historicengland.org.uk/listing/the-list/list-entry/",
            params={"National_Park": "", "search_term": kw.split()[0], "pageSize": 3},
            headers={"User-Agent": "TruthScore/4.0", "Accept": "application/json"})
        sources = []
        if r2.status_code == 200:
            try:
                for entry in r2.json().get("entries",[])[:3]:
                    title = entry.get("name","")[:120]
                    desc  = entry.get("summary","")[:280]
                    url   = entry.get("url","https://historicengland.org.uk")
                    if title:
                        sources.append(Source(type="academic", title=title, url=url,
                            snippet=desc or "Historic England listed entry",
                            publisher="Historic England"))
            except Exception:
                pass
        # If API fails, at least return a reference link
        if not sources and kw:
            sources.append(Source(type="academic",
                title=f"Historic England: {kw[:60]}",
                url=f"https://historicengland.org.uk/listing/the-list/?searchType=nhle&q={kw.replace(' ','+')}",
                snippet=f"Search Historic England's National Heritage List for: {kw}",
                publisher="Historic England"))
        print(f"  [HISTORIC_ENGLAND] {len(sources)} results")
        return sources[:2]



# ── USGS -- earthquakes, geology, topography (free, no key) ───────────────────
async def search_usgs(claim: str) -> list[Source]:
    """USGS -- US Geological Survey. Earthquakes, geology, elevation, water data."""
    kw = extract_keywords(claim)
    if not kw: return []
    sources = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # USGS Science Data Catalog
        r = await client.get("https://www.sciencebase.gov/catalog/items",
            params={"q": kw, "max": 4, "format": "json",
                    "fields": "title,body,webLinks,summary"},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code == 200:
            for item in r.json().get("items",[])[:3]:
                title   = item.get("title","")[:120]
                summary = item.get("summary","") or item.get("body","")
                summary = re.sub(r"<[^>]+>","",summary)[:280]
                links   = item.get("webLinks",[])
                url     = links[0].get("uri","https://www.usgs.gov") if links else "https://www.usgs.gov"
                if title:
                    sources.append(Source(type="academic", title=title, url=url,
                        snippet=summary or "USGS scientific data record",
                        publisher="USGS -- US Geological Survey"))

        # Also try earthquake data if claim mentions earthquakes
        if any(w in kw.lower() for w in ["earthquake","seismic","richter","tremor","fault","tectonic"]):
            r2 = await client.get("https://earthquake.usgs.gov/fdsnws/event/1/query",
                params={"format":"geojson","limit":3,"orderby":"magnitude",
                        "minmagnitude":5.0})
            if r2.status_code == 200:
                for eq in r2.json().get("features",[])[:2]:
                    props = eq.get("properties",{})
                    mag   = props.get("mag","")
                    place = props.get("place","")
                    time  = props.get("time","")
                    url   = props.get("url","https://earthquake.usgs.gov")
                    if place:
                        sources.append(Source(type="academic",
                            title=f"M{mag} earthquake: {place}",
                            url=url, snippet=f"Magnitude {mag} earthquake near {place}",
                            publisher="USGS Earthquake Hazards Program"))
    print(f"  [USGS] {len(sources)} results")
    return sources[:3]


# ── Library of Congress -- books, historic documents (free, no key) ───────────
async def search_loc(claim: str) -> list[Source]:
    """Library of Congress -- 17M+ items: books, manuscripts, maps, photos."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://www.loc.gov/search/",
            params={"q": kw, "fo": "json", "c": 4, "at": "results"},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code != 200: return []
        sources = []
        for item in r.json().get("results",[])[:4]:
            title = item.get("title","")[:120]
            desc  = item.get("description",[""])
            desc  = desc[0] if isinstance(desc,list) and desc else str(desc)
            desc  = re.sub(r"<[^>]+>","",desc)[:280]
            url   = item.get("url","https://loc.gov") or "https://loc.gov"
            date  = item.get("date","")
            if not title: continue
            snippet = f"{desc}" if desc else ""
            if date: snippet = f"({date}) {snippet}"
            sources.append(Source(type="academic", title=title, url=url,
                snippet=snippet[:300], publisher="Library of Congress"))
        print(f"  [LOC] {len(sources)} results")
        return sources[:3]


# ── EPA -- environmental data (free, no key) ────────────────────────────────
async def search_epa(claim: str) -> list[Source]:
    """EPA -- US Environmental Protection Agency data."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://www.epa.gov/search/",
            params={"query": kw, "collection": "epa_web", "rows": 4},
            headers={"User-Agent": "TruthScore/4.0", "Accept": "application/json"})
        sources = []
        if r.status_code == 200:
            try:
                for item in r.json().get("results",{}).get("result",[])[:3]:
                    title   = item.get("title","")[:120]
                    snippet = item.get("snippet","")[:280]
                    url     = item.get("url","https://www.epa.gov")
                    if title:
                        sources.append(Source(type="academic", title=title, url=url,
                            snippet=snippet, publisher="US EPA"))
            except Exception:
                pass
        if not sources:
            # Simple fallback reference
            sources.append(Source(type="academic",
                title=f"EPA Data: {kw[:60]}",
                url=f"https://www.epa.gov/search-results?q={kw.replace(' ','+')}",
                snippet="US Environmental Protection Agency official data",
                publisher="US EPA"))
        print(f"  [EPA] {len(sources)} results")
        return sources[:2]



# ── UNESCO Open Data Platform -- data.unesco.org/api/explore/v2.1 ─────────────
async def search_unesco_data(claim: str) -> list[Source]:
    """
    UNESCO Open Data (data.unesco.org) -- Explore API v2.1
    Contains datasets on: World Heritage Sites, education statistics,
    cultural diversity, science, media freedom, demographic data.
    Free, no key required.
    """
    kw = extract_keywords(claim)
    if not kw: return []
    BASE = "https://data.unesco.org/api/explore/v2.1"
    sources = []

    async with httpx.AsyncClient(timeout=12.0) as client:

        # Step 1: Find relevant datasets
        r = await client.get(f"{BASE}/catalog/datasets",
            params={"where": f'title like "%{kw.split()[0]}%"',
                    "limit": 5, "lang": "en"},
            headers={"User-Agent": "TruthScore/4.0"})

        datasets = []
        if r.status_code == 200:
            for ds in r.json().get("results", [])[:3]:
                ds_id = ds.get("dataset_id","")
                title = ds.get("metas",{}).get("default",{}).get("title","") or ds_id
                if ds_id:
                    datasets.append((ds_id, title))

        # Step 2: Also try World Heritage Sites dataset directly
        wh_datasets = [
            ("world-heritage-inscription-new", "UNESCO World Heritage Sites"),
            ("whc-sites-2021", "UNESCO World Heritage Sites 2021"),
        ]
        for ds_id, ds_title in wh_datasets:
            if ("heritage" in kw.lower() or "historic" in kw.lower() or
                "monument" in kw.lower() or "site" in kw.lower() or
                any(w in kw.lower() for w in ["china","wall","great","rome","pyramid"])):
                datasets.insert(0, (ds_id, ds_title))

        # Step 3: Query records from found datasets
        for ds_id, ds_title in datasets[:3]:
            r2 = await client.get(f"{BASE}/catalog/datasets/{ds_id}/records",
                params={"where": f'search(name_en,"{kw.split()[0]}") OR search(short_description_en,"{kw.split()[0]}")',
                        "limit": 3, "lang": "en"},
                headers={"User-Agent": "TruthScore/4.0"})

            if r2.status_code == 200:
                records = r2.json().get("results", [])
                for rec in records[:2]:
                    # Try common field names across UNESCO datasets
                    name = (rec.get("name_en") or rec.get("name") or
                            rec.get("site") or rec.get("title",""))[:120]
                    desc = (rec.get("short_description_en") or
                            rec.get("description") or rec.get("justification",""))
                    desc = re.sub(r"<[^>]+>", "", str(desc or ""))[:300]
                    country = rec.get("states_name_en") or rec.get("country","")
                    category = rec.get("category") or rec.get("type","")
                    year = rec.get("date_inscribed") or rec.get("year","")

                    if not name or len(name) < 3: continue
                    snippet = []
                    if country:  snippet.append(f"Country: {country}")
                    if category: snippet.append(f"Category: {category}")
                    if year:     snippet.append(f"Year: {year}")
                    if desc:     snippet.append(desc)

                    sources.append(Source(
                        type="academic",
                        title=f"{ds_title}: {name}",
                        url=f"https://data.unesco.org/explore/dataset/{ds_id}/",
                        snippet=". ".join(snippet)[:300],
                        publisher="UNESCO Open Data Platform"
                    ))

        # Step 4: If no records found, do a full-text catalog search
        if not sources:
            r3 = await client.get(f"{BASE}/catalog/datasets",
                params={"search": kw, "limit": 4},
                headers={"User-Agent": "TruthScore/4.0"})
            if r3.status_code == 200:
                for ds in r3.json().get("results", [])[:3]:
                    ds_id   = ds.get("dataset_id","")
                    metas   = ds.get("metas",{}).get("default",{})
                    title   = metas.get("title","")[:120]
                    desc    = metas.get("description","")[:250]
                    records = ds.get("metas",{}).get("processing",{}).get("records_count",0)
                    if not title: continue
                    sources.append(Source(
                        type="academic",
                        title=f"UNESCO Dataset: {title}",
                        url=f"https://data.unesco.org/explore/dataset/{ds_id}/",
                        snippet=f"{desc} ({records:,} records)" if desc else f"{records:,} records",
                        publisher="UNESCO Open Data Platform"
                    ))

    print(f"  [UNESCO-DATA] {len(sources)} results for {kw!r}")
    return sources[:4]



# ══════════════════════════════════════════════════════════════
# MEDICAL APIs -- authoritative clinical and health data
# ══════════════════════════════════════════════════════════════

# ── WHO Global Health Observatory (free, no key) ──────────────
async def search_who(claim: str) -> list[Source]:
    """WHO GHO -- global health statistics, disease data, mortality."""
    kw = extract_keywords(claim)
    if not kw: return []
    sources = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # WHO GHO ODATA API
        r = await client.get(
            "https://ghoapi.azureedge.net/api/Indicator",
            params={"$filter": f"contains(IndicatorName, '{kw.split()[0]}')",
                    "$top": 4},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code == 200:
            for ind in r.json().get("value", [])[:3]:
                name = ind.get("IndicatorName","")[:120]
                code = ind.get("IndicatorCode","")
                if not name: continue
                sources.append(Source(type="academic",
                    title=f"WHO Indicator: {name}",
                    url=f"https://www.who.int/data/gho/data/indicators/indicator-details/GHO/{code}",
                    snippet=f"WHO Global Health Observatory indicator: {name}",
                    publisher="WHO -- World Health Organization"))

        # WHO disease outbreak news
        r2 = await client.get(
            "https://www.who.int/api/news/newsitems",
            params={"sf_culture": "en", "$filter": f"contains(Title,'{kw.split()[0]}')",
                    "$top": 3},
            headers={"User-Agent": "TruthScore/4.0"})
        if r2.status_code == 200:
            try:
                for item in r2.json().get("value",[])[:2]:
                    title = item.get("Title","")[:120]
                    url   = item.get("Url","https://www.who.int")
                    date  = item.get("PublicationDateAndTime","")[:10]
                    if title:
                        sources.append(Source(type="news", title=title,
                            url=f"https://www.who.int{url}" if url.startswith("/") else url,
                            snippet=f"WHO health news ({date})",
                            publisher="WHO -- World Health Organization"))
            except Exception: pass

    print(f"  [WHO] {len(sources)} results")
    return sources[:3]


# ── CDC -- Centers for Disease Control (free, no key) ──────────
async def search_cdc(claim: str) -> list[Source]:
    """CDC Open Data -- disease statistics, vaccination rates, public health."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # CDC WONDER / Socrata API
        r = await client.get(
            "https://data.cdc.gov/api/views/metadata/v1",
            params={"search": kw, "limit": 4},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            try:
                items = r.json() if isinstance(r.json(), list) else []
                for item in items[:3]:
                    title   = item.get("name","")[:120]
                    desc    = item.get("description","")[:280]
                    uid     = item.get("id","")
                    if title:
                        sources.append(Source(type="academic",
                            title=f"CDC Dataset: {title}",
                            url=f"https://data.cdc.gov/d/{uid}" if uid else "https://data.cdc.gov",
                            snippet=desc or "CDC public health dataset",
                            publisher="CDC -- Centers for Disease Control"))
            except Exception: pass

        # CDC MMWR articles via search
        if not sources:
            r2 = await client.get(
                f"https://www.cdc.gov/search/#/?query={kw.replace(' ','+')}",
                headers={"User-Agent": "TruthScore/4.0",
                         "Accept": "application/json, text/plain"})
            if r2.status_code == 200:
                sources.append(Source(type="academic",
                    title=f"CDC Health Information: {kw[:60]}",
                    url=f"https://www.cdc.gov/search/#/?query={kw.replace(' ','+')}",
                    snippet="Official CDC public health information and statistics",
                    publisher="CDC -- Centers for Disease Control"))

    print(f"  [CDC] {len(sources)} results")
    return sources[:3]


# ── ClinicalTrials.gov -- real clinical studies (free, no key) ─
async def search_clinicaltrials(claim: str) -> list[Source]:
    """ClinicalTrials.gov -- 400k+ clinical trials, treatments, outcomes."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(
            "https://clinicaltrials.gov/api/v2/studies",
            params={"query.term": kw, "pageSize": 4,
                    "fields": "NCTId,BriefTitle,BriefSummary,OverallStatus,Phase,Condition"},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code != 200: return []
        sources = []
        for study in r.json().get("studies", [])[:4]:
            proto  = study.get("protocolSection",{})
            ident  = proto.get("identificationModule",{})
            desc   = proto.get("descriptionModule",{})
            status = proto.get("statusModule",{})
            conds  = proto.get("conditionsModule",{})

            title   = ident.get("briefTitle","")[:120]
            nct_id  = ident.get("nctId","")
            summary = desc.get("briefSummary","")[:300]
            phase   = status.get("phase","")
            overall = status.get("overallStatus","")
            cond    = ", ".join(conds.get("conditions",[])[:3])

            if not title: continue
            snippet = f"Status: {overall}" if overall else ""
            if phase:   snippet += f", Phase: {phase}"
            if cond:    snippet += f". Conditions: {cond}"
            if summary: snippet += f". {summary[:200]}"
            sources.append(Source(type="academic", title=title,
                url=f"https://clinicaltrials.gov/study/{nct_id}",
                snippet=snippet[:350], publisher="ClinicalTrials.gov"))
        print(f"  [CLINICALTRIALS] {len(sources)} studies")
        return sources[:3]


# ── NCBI / Entrez -- genetics, proteins, DNA (same system as PubMed) ─────────
async def search_ncbi(claim: str) -> list[Source]:
    """NCBI Entrez -- genetics, molecular biology, proteins, disease genes."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        # Search Gene database
        r = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "gene", "term": kw, "retmax": 3,
                    "retmode": "json", "tool": "TruthScore"})
        sources = []
        if r.status_code == 200:
            ids = r.json().get("esearchresult",{}).get("idlist",[])
            if ids:
                r2 = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    params={"db": "gene", "id": ",".join(ids[:3]),
                            "retmode": "json", "tool": "TruthScore"})
                if r2.status_code == 200:
                    result = r2.json().get("result",{})
                    for gid in ids[:3]:
                        g = result.get(gid,{})
                        name = g.get("name","")
                        desc = g.get("description","")[:200]
                        org  = g.get("organism",{}).get("scientificname","")
                        if name:
                            sources.append(Source(type="academic",
                                title=f"Gene: {name} -- {desc[:60]}",
                                url=f"https://www.ncbi.nlm.nih.gov/gene/{gid}",
                                snippet=f"{desc}. Organism: {org}" if org else desc,
                                publisher="NCBI Gene Database"))

        # Also search MeSH terms for medical vocabulary
        if not sources:
            r3 = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "mesh", "term": kw, "retmax": 2,
                        "retmode": "json", "tool": "TruthScore"})
            if r3.status_code == 200:
                ids = r3.json().get("esearchresult",{}).get("idlist",[])
                if ids:
                    sources.append(Source(type="academic",
                        title=f"MeSH Medical Term: {kw[:60]}",
                        url=f"https://www.ncbi.nlm.nih.gov/mesh/?term={kw.replace(' ','+')}",
                        snippet=f"Medical Subject Heading (MeSH) vocabulary entry for: {kw}",
                        publisher="NCBI MeSH -- Medical Subject Headings"))

    print(f"  [NCBI] {len(sources)} results")
    return sources[:3]



# ══════════════════════════════════════════════════════════════
# MATH APIs
# ══════════════════════════════════════════════════════════════

# ── Wolfram Alpha -- computational knowledge (free key) ────────
async def search_wolfram(claim: str) -> list[Source]:
    """Wolfram Alpha -- symbolic math, equations, facts, computations."""
    key = os.getenv("WOLFRAM_API_KEY", os.getenv("WOLFRAM", ""))
    if not key: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get("https://api.wolframalpha.com/v2/query",
            params={"input": claim[:200], "appid": key,
                    "output": "json", "format": "plaintext",
                    "podstate": "Step-by-step solution"},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code != 200: return []
        pods = r.json().get("queryresult",{}).get("pods",[])
        sources = []
        for pod in pods[:5]:
            title = pod.get("title","")
            subs  = pod.get("subpods",[])
            for sub in subs[:2]:
                text = sub.get("plaintext","").strip()
                if not text or len(text) < 3: continue
                sources.append(Source(type="academic",
                    title=f"Wolfram Alpha: {title}",
                    url=f"https://www.wolframalpha.com/input?i={claim[:100].replace(' ','+')}",
                    snippet=text[:300],
                    publisher="Wolfram Alpha -- Computational Intelligence"))
            if len(sources) >= 3: break
        print(f"  [WOLFRAM] {len(sources)} results")
        return sources[:3]


# ── OpenAlex -- 250M academic papers including math ────────────
async def search_openalex_math(claim: str) -> list[Source]:
    """OpenAlex filtered to mathematics journals."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.openalex.org/works",
            params={"search": kw, "per-page": 4,
                    "filter": "primary_topic.field.display_name:Mathematics",
                    "select": "title,doi,publication_year,abstract_inverted_index,primary_location",
                    "mailto": "thesis@example.com"})
        if r.status_code != 200: return []
        sources = []
        for w in r.json().get("results",[])[:4]:
            title   = w.get("title","")[:120]
            doi     = w.get("doi","")
            year    = w.get("publication_year","")
            venue   = ((w.get("primary_location") or {}).get("source") or {})
            journal = venue.get("display_name","Mathematics Journal")
            abstract= _reconstruct_abstract(w.get("abstract_inverted_index"))
            if not title: continue
            sources.append(Source(type="academic",
                title=f"{title[:100]} ({year})",
                url=doi or "https://openalex.org",
                snippet=abstract[:350] if abstract else f"Mathematics paper: {title}",
                publisher=f"{journal} -- OpenAlex"))
        print(f"  [OPENALEX-MATH] {len(sources)} results")
        return sources[:3]


# ══════════════════════════════════════════════════════════════
# SPORT APIs
# ══════════════════════════════════════════════════════════════

# ── football-data.org -- real football stats (free key) ────────
async def search_football_data(claim: str) -> list[Source]:
    """football-data.org -- competitions, teams, standings, scorers."""
    key = os.getenv("FOOTBALL_DATA_KEY", os.getenv("FOOTBALLDATA", ""))
    if not key: return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Search competitions
        r = await client.get("https://api.football-data.org/v4/competitions",
            headers={"X-Auth-Token": key, "User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            comps = r.json().get("competitions",[])
            kw_lower = kw.lower()
            for comp in comps:
                name = comp.get("name","").lower()
                area = comp.get("area",{}).get("name","")
                if any(w in name for w in kw_lower.split()):
                    sources.append(Source(type="news",
                        title=f"Football: {comp.get('name','')}",
                        url=f"https://www.football-data.org",
                        snippet=f"Competition: {comp.get('name')}, Area: {area}, Season: {comp.get('currentSeason',{}).get('startDate','')}",
                        publisher="football-data.org"))
                    if len(sources) >= 2: break

        # Top scorers if claim mentions goals/scorer
        if any(w in claim.lower() for w in ["goal","scorer","scored","goals","top scorer"]):
            r2 = await client.get("https://api.football-data.org/v4/competitions/PL/scorers",
                headers={"X-Auth-Token": key})
            if r2.status_code == 200:
                for scorer in r2.json().get("scorers",[])[:3]:
                    p = scorer.get("player",{})
                    g = scorer.get("goals",0)
                    t = scorer.get("team",{}).get("name","")
                    sources.append(Source(type="news",
                        title=f"{p.get('name','')} -- {g} goals ({t})",
                        url="https://www.football-data.org/v4/competitions/PL/scorers",
                        snippet=f"Premier League top scorer: {p.get('name')} with {g} goals for {t}",
                        publisher="football-data.org"))
        print(f"  [FOOTBALL-DATA] {len(sources)} results")
        return sources[:3]


# ── balldontlie -- NBA stats (free, no key) ────────────────────
async def search_nba_stats(claim: str) -> list[Source]:
    """balldontlie.io -- NBA players, teams, game stats. No key needed."""
    kw = extract_keywords(claim)
    if not kw: return []
    # Only activate for NBA/basketball claims
    if not any(w in claim.lower() for w in ["nba","basketball","lakers","bulls","celtics",
        "warrior","heat","bucks","player","lebron","jordan","curry","durant","kobe"]):
        return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://www.balldontlie.io/api/v1/players",
            params={"search": kw.split()[0], "per_page": 3},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            for p in r.json().get("data",[])[:3]:
                name = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
                pos  = p.get("position","")
                team = p.get("team",{}).get("full_name","")
                conf = p.get("team",{}).get("conference","")
                if name and name != " ":
                    sources.append(Source(type="news",
                        title=f"NBA Player: {name}",
                        url="https://www.balldontlie.io",
                        snippet=f"Position: {pos}, Team: {team}, Conference: {conf}",
                        publisher="balldontlie.io -- NBA Stats"))
        print(f"  [NBA] {len(sources)} results")
        return sources[:3]


# ── TheSportsDB -- multi-sport (free tier) ─────────────────────
async def search_sportsdb(claim: str) -> list[Source]:
    """TheSportsDB -- teams, players, events across all sports."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Search teams
        r = await client.get(
            f"https://www.thesportsdb.com/api/v1/json/3/searchteams.php",
            params={"t": kw.split()[0]},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200 and r.json().get("teams"):
            for team in (r.json().get("teams") or [])[:2]:
                name    = team.get("strTeam","")[:80]
                sport   = team.get("strSport","")
                country = team.get("strCountry","")
                formed  = team.get("intFormedYear","")
                desc    = team.get("strDescriptionEN","")[:250]
                if name:
                    sources.append(Source(type="news",
                        title=f"{sport}: {name}",
                        url=f"https://www.thesportsdb.com/team/{team.get('idTeam','')}",
                        snippet=f"Sport: {sport}, Country: {country}, Founded: {formed}. {desc}",
                        publisher="TheSportsDB"))

        # Search players
        r2 = await client.get(
            "https://www.thesportsdb.com/api/v1/json/3/searchplayers.php",
            params={"p": kw.split()[0]},
            headers={"User-Agent": "TruthScore/4.0"})
        if r2.status_code == 200 and r2.json().get("player"):
            for p in (r2.json().get("player") or [])[:2]:
                name   = p.get("strPlayer","")[:80]
                sport  = p.get("strSport","")
                team   = p.get("strTeam","")
                nat    = p.get("strNationality","")
                pos    = p.get("strPosition","")
                birth  = p.get("dateBorn","")[:10]
                desc   = p.get("strDescriptionEN","")[:200]
                if name:
                    sources.append(Source(type="news",
                        title=f"{sport} player: {name}",
                        url=f"https://www.thesportsdb.com/player/{p.get('idPlayer','')}",
                        snippet=f"Team: {team}, Nationality: {nat}, Position: {pos}, Born: {birth}. {desc}",
                        publisher="TheSportsDB"))
        print(f"  [SPORTSDB] {len(sources)} results")
        return sources[:4]


# ── Ergast -- Formula 1 history (free, no key) ─────────────────
async def search_f1(claim: str) -> list[Source]:
    """Ergast Motor Racing -- F1 results, drivers, constructors."""
    if not any(w in claim.lower() for w in
               ["formula","f1","ferrari","mercedes","hamilton","verstappen",
                "race","circuit","grand prix","constructor","driver"]):
        return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Driver search
        r = await client.get(
            f"https://ergast.com/api/f1/drivers.json",
            params={"limit": 5},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            drivers = r.json().get("MRData",{}).get("DriverTable",{}).get("Drivers",[])
            kw_low  = kw.lower()
            for d in drivers:
                full = f"{d.get('givenName','')} {d.get('familyName','')}".lower()
                if any(w in full for w in kw_low.split()):
                    sources.append(Source(type="news",
                        title=f"F1 Driver: {d.get('givenName','')} {d.get('familyName','')}",
                        url=d.get("url","https://ergast.com"),
                        snippet=f"Nationality: {d.get('nationality','')}, "
                                f"Code: {d.get('code','')}, "
                                f"DOB: {d.get('dateOfBirth','')}",
                        publisher="Ergast Motor Racing -- F1 Data"))
                if len(sources) >= 2: break
        print(f"  [F1] {len(sources)} results")
        return sources[:2]


# ══════════════════════════════════════════════════════════════
# POLITICS APIs
# ══════════════════════════════════════════════════════════════

# ── EU Open Data Portal (free, no key) ────────────────────────
async def search_eu_data(claim: str) -> list[Source]:
    """EU Open Data Portal -- EU laws, policies, statistics, regulations."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://data.europa.eu/api/hub/search/search",
            params={"q": kw, "limit": 4, "filter": "dataset",
                    "facets": "country,theme", "lang": "en"},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            for item in r.json().get("result",{}).get("results",[])[:4]:
                title = item.get("title","")
                if isinstance(title, dict): title = title.get("en","")
                title = str(title)[:120]
                desc  = item.get("description","")
                if isinstance(desc, dict): desc = desc.get("en","")
                desc  = str(desc)[:250]
                url   = item.get("landingPage","")
                if isinstance(url, list): url = url[0] if url else "https://data.europa.eu"
                if not title: continue
                sources.append(Source(type="academic", title=title, url=url,
                    snippet=desc or "EU Open Data dataset",
                    publisher="EU Open Data Portal -- data.europa.eu"))
        print(f"  [EU-DATA] {len(sources)} results")
        return sources[:3]


# ── GovTrack -- US Congress bills and votes (free, no key) ───
async def search_govtrack(claim: str) -> list[Source]:
    """GovTrack.us -- US Congress legislation, voting records, members. Free, no key."""
    if not any(w in claim.lower() for w in
               ["congress","senate","house","bill","vote","law","legislation",
                "republican","democrat","president","government","policy","amendment"]):
        return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://www.govtrack.us/api/v2/bill",
            params={"q": kw, "limit": 4, "order_by": "-current_status_date"},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            for bill in r.json().get("objects",[])[:4]:
                title   = bill.get("title","")[:120]
                btype   = bill.get("bill_type_label","")
                number  = bill.get("number","")
                status  = bill.get("current_status_label","")
                chamber = bill.get("originating_chamber_label","")
                url     = f"https://www.govtrack.us{bill.get('link','')}"
                if title:
                    sources.append(Source(type="news",
                        title=f"US {btype} {number}: {title[:80]}",
                        url=url,
                        snippet=f"Status: {status}. Chamber: {chamber}",
                        publisher="GovTrack.us -- US Congress"))
        print(f"  [GOVTRACK] {len(sources)} results")
        return sources[:3]


# ── OpenStates -- state legislatures (free key) ────────────────
async def search_openstates(claim: str) -> list[Source]:
    """OpenStates -- US state legislation, bills, legislators."""
    key = os.getenv("OPENSTATES_API_KEY", os.getenv("OPENSTATES", ""))
    if not key: return []
    if not any(w in claim.lower() for w in
               ["state","law","legislation","bill","legislature","governor"]):
        return []
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://v3.openstates.org/bills",
            params={"q": kw, "per_page": 3, "sort": "updated_desc"},
            headers={"X-API-KEY": key, "User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            for bill in r.json().get("results",[])[:3]:
                title  = bill.get("title","")[:120]
                state  = bill.get("jurisdiction",{}).get("name","")
                status = bill.get("latest_action_description","")
                date   = bill.get("latest_action_date","")[:10]
                url    = bill.get("openstates_url","https://openstates.org")
                if title:
                    sources.append(Source(type="news",
                        title=f"State Bill: {title}",
                        url=url,
                        snippet=f"State: {state}, Latest: {status} ({date})",
                        publisher="OpenStates"))
        print(f"  [OPENSTATES] {len(sources)} results")
        return sources[:3]



# ══════════════════════════════════════════════════════════════
# LOGIC / PHILOSOPHY / SOCIAL SCIENCES / PSYCHOLOGY / NUTRITION
# ══════════════════════════════════════════════════════════════

# ── Stanford Encyclopedia of Philosophy (free, no key) ────────
async def search_sep(claim: str) -> list[Source]:
    """Stanford Encyclopedia of Philosophy -- authoritative philosophy & logic."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://plato.stanford.edu/cgi-bin/encyclopedia/search",
            params={"query": kw, "search": "Search"},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            entries = re.findall(
                r'<a[^>]+href="(/entries/[^"]+)"[^>]*>([^<]+)</a>',
                r.text)
            for path, title in entries[:4]:
                title = title.strip()[:120]
                if len(title) > 3:
                    sources.append(Source(type="academic",
                        title=f"SEP: {title}",
                        url=f"https://plato.stanford.edu{path}",
                        snippet=f"Stanford Encyclopedia of Philosophy entry on: {title}",
                        publisher="Stanford Encyclopedia of Philosophy"))
        # Fallback: direct URL if known topic
        if not sources:
            slug = kw.replace(" ","-").lower()
            r2 = await client.get(f"https://plato.stanford.edu/entries/{slug}/")
            if r2.status_code == 200:
                title_m = re.search(r"<title>(.*?)</title>", r2.text)
                title   = title_m.group(1).replace(" (Stanford Encyclopedia of Philosophy)","").strip() if title_m else kw
                first_p = re.search(r"<div id=\"preamble\".*?<p>(.*?)</p>", r2.text, re.DOTALL)
                snippet = re.sub(r"<[^>]+>","", first_p.group(1) if first_p else "")[:300]
                sources.append(Source(type="academic",
                    title=f"SEP: {title[:120]}",
                    url=f"https://plato.stanford.edu/entries/{slug}/",
                    snippet=snippet,
                    publisher="Stanford Encyclopedia of Philosophy"))
    print(f"  [SEP] {len(sources)} results")
    return sources[:3]


# ── NASA ADS -- astronomy & astrophysics papers (free, key optional) ──────────
async def search_nasa_ads(claim: str) -> list[Source]:
    """NASA Astrophysics Data System -- 15M+ astronomy papers."""
    kw = extract_keywords(claim)
    if not kw: return []
    key = os.getenv("NASA_ADS_KEY", "")
    headers = {"User-Agent": "TruthScore/4.0"}
    if key: headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get("https://api.adsabs.harvard.edu/v1/search/query",
            params={"q": kw, "fl": "title,abstract,author,year,bibcode",
                    "rows": 4, "sort": "score desc"},
            headers=headers)
        sources = []
        if r.status_code == 200:
            for doc in r.json().get("response",{}).get("docs",[])[:4]:
                titles  = doc.get("title",[])
                title   = titles[0][:120] if titles else ""
                abstract= doc.get("abstract","")[:300]
                authors = doc.get("author",[])[:2]
                year    = doc.get("year","")
                bibcode = doc.get("bibcode","")
                if not title: continue
                sources.append(Source(type="academic",
                    title=f"{title} ({year})",
                    url=f"https://ui.adsabs.harvard.edu/abs/{bibcode}",
                    snippet=abstract or f"Astronomy paper by {', '.join(authors)}",
                    publisher="NASA ADS -- Astrophysics Data System"))
        print(f"  [NASA-ADS] {len(sources)} results")
        return sources[:3]


# ── PsycINFO via Semantic Scholar filter (free) ───────────────
async def search_psychology(claim: str) -> list[Source]:
    """Psychology papers via Semantic Scholar filtered to psych fields."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": kw, "limit": 5,
                    "fields": "title,abstract,year,authors,url,venue",
                    "fieldsOfStudy": "Psychology,Medicine"},
            headers={"User-Agent": "TruthScore/4.0"})
        sources = []
        if r.status_code == 200:
            for p in r.json().get("data",[])[:4]:
                title    = (p.get("title") or "")[:120]
                abstract = (p.get("abstract") or "")[:350]
                year     = p.get("year","")
                venue    = p.get("venue","Psychology Journal")
                authors  = ", ".join(a.get("name","") for a in (p.get("authors") or [])[:2])
                url      = p.get("url","https://www.semanticscholar.org")
                if not title: continue
                sources.append(Source(type="academic",
                    title=f"{title} ({year})",
                    url=url, snippet=abstract,
                    publisher=f"{authors} -- {venue}" if authors else venue))
        print(f"  [PSYCH] {len(sources)} results")
        return sources[:3]


# ── Sociology / Social Sciences via OpenAlex (free) ───────────
async def search_social_sciences(claim: str) -> list[Source]:
    """Sociology and social science papers via OpenAlex."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.openalex.org/works",
            params={"search": kw, "per-page": 4,
                    "filter": "primary_topic.field.display_name:Social Sciences",
                    "select": "title,doi,publication_year,abstract_inverted_index,primary_location",
                    "mailto": "thesis@example.com"})
        sources = []
        if r.status_code == 200:
            for w in r.json().get("results",[])[:4]:
                title    = w.get("title","")[:120]
                doi      = w.get("doi","")
                year     = w.get("publication_year","")
                venue    = ((w.get("primary_location") or {}).get("source") or {})
                journal  = venue.get("display_name","Social Science Journal")
                abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
                if not title: continue
                sources.append(Source(type="academic",
                    title=f"{title[:100]} ({year})",
                    url=doi or "https://openalex.org",
                    snippet=abstract[:350] if abstract else f"Social science paper: {title}",
                    publisher=f"{journal} -- OpenAlex"))
        print(f"  [SOCIAL-SCI] {len(sources)} results")
        return sources[:3]


# ── Religion / Theology -- Oxford Reference + CrossRef (free) ──
async def search_religion(claim: str) -> list[Source]:
    """Religion and theology via CrossRef + DDG restricted to religious encyclopedias."""
    kw = extract_keywords(claim)
    if not kw: return []
    sources = []
    async with httpx.AsyncClient(timeout=12.0) as client:
        # CrossRef theology papers
        r = await client.get("https://api.crossref.org/works",
            params={"query": kw, "rows": 3,
                    "query.container-title": "theology religion ethics scripture",
                    "select": "title,abstract,URL,published,author,container-title",
                    "mailto": "thesis@example.com"})
        if r.status_code == 200:
            for item in r.json().get("message",{}).get("items",[])[:3]:
                titles  = item.get("title",[])
                title   = titles[0][:120] if titles else ""
                journal = (item.get("container-title") or [""])
                journal = journal[0] if journal else "Theology Journal"
                abstract= re.sub(r"<[^>]+>","",item.get("abstract",""))[:280]
                url     = item.get("URL","")
                year    = item.get("published",{}).get("date-parts",[[""]])[0][0]
                if title:
                    sources.append(Source(type="academic",
                        title=f"{title} ({year})",
                        url=url, snippet=abstract or f"Academic article in {journal}",
                        publisher=f"{journal}"))

        # World Religion Database via Wikidata
        r2 = await client.get("https://www.wikidata.org/w/api.php",
            params={"action":"wbsearchentities","search":kw,
                    "language":"en","format":"json","limit":3},
            headers={"User-Agent": "TruthScore/4.0"})
        if r2.status_code == 200:
            for item in r2.json().get("search",[])[:2]:
                label = item.get("label","")
                desc  = item.get("description","")
                qid   = item.get("id","")
                if label and any(w in desc.lower() for w in
                    ["religion","religious","theology","faith","scripture","prophet",
                     "church","mosque","temple","god","deity","sacred","spiritual"]):
                    sources.append(Source(type="wikidata",
                        title=label, url=f"https://www.wikidata.org/wiki/{qid}",
                        snippet=f"{label}: {desc}",
                        publisher="Wikidata -- Religion & Theology"))
    print(f"  [RELIGION] {len(sources)} results")
    return sources[:4]


# ── Nutrition -- USDA FoodData Central (free, no key) ──────────
async def search_nutrition(claim: str) -> list[Source]:
    """USDA FoodData Central -- nutritional composition of 400k+ foods."""
    kw = extract_keywords(claim)
    if not kw: return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # USDA FoodData search
        r = await client.get("https://api.nal.usda.gov/fdc/v1/foods/search",
            params={"query": kw, "pageSize": 4,
                    "api_key": os.getenv("USDA_API_KEY", os.getenv("USDA", "DEMO_KEY"))})
        sources = []
        if r.status_code == 200:
            for food in r.json().get("foods",[])[:3]:
                name       = food.get("description","")[:120]
                brand      = food.get("brandOwner","")
                category   = food.get("foodCategory","")
                nutrients  = food.get("foodNutrients",[])
                key_nutrs  = []
                for n in nutrients[:8]:
                    nname = n.get("nutrientName","")
                    val   = n.get("value","")
                    unit  = n.get("unitName","")
                    if nname and val and any(x in nname.lower() for x in
                        ["calorie","protein","fat","carbohydrate","fiber","sugar","vitamin","calcium","iron"]):
                        key_nutrs.append(f"{nname}: {val}{unit}")
                snippet = f"Category: {category}." if category else ""
                if brand:     snippet += f" Brand: {brand}."
                if key_nutrs: snippet += " " + ", ".join(key_nutrs[:5])
                if not name: continue
                sources.append(Source(type="academic",
                    title=f"Nutrition: {name}",
                    url=f"https://fdc.nal.usda.gov/food-details/{food.get('fdcId','')}/nutrients",
                    snippet=snippet[:350],
                    publisher="USDA FoodData Central"))

        # Also: nutrition science papers via PubMed
        r2 = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db":"pubmed","term":f"{kw} nutrition diet",
                    "retmax":2,"retmode":"json","tool":"TruthScore"})
        if r2.status_code == 200:
            ids = r2.json().get("esearchresult",{}).get("idlist",[])
            if ids:
                sources.append(Source(type="academic",
                    title=f"PubMed Nutrition Research: {kw[:60]}",
                    url=f"https://pubmed.ncbi.nlm.nih.gov/?term={kw.replace(' ','+')}+nutrition",
                    snippet=f"{len(ids)}+ nutrition science papers on PubMed for: {kw}",
                    publisher="PubMed -- NCBI Nutrition Research"))
    print(f"  [NUTRITION] {len(sources)} results")
    return sources[:4]


# ── Business / Economics -- OpenCorporates + SEC EDGAR (free) ──
async def search_business(claim: str) -> list[Source]:
    """Business data: companies, filings, financial reports."""
    kw = extract_keywords(claim)
    if not kw: return []
    sources = []
    async with httpx.AsyncClient(timeout=12.0) as client:
        # SEC EDGAR full-text search (US companies)
        r = await client.get("https://efts.sec.gov/LATEST/search-index?q=" +
            kw.replace(" ","+") + "&dateRange=custom&startdt=2020-01-01&forms=10-K,10-Q",
            headers={"User-Agent": "TruthScore/4.0 admin@example.com"})
        if r.status_code == 200:
            try:
                hits = r.json().get("hits",{}).get("hits",[])
                for hit in hits[:3]:
                    src     = hit.get("_source",{})
                    title   = src.get("display_names",[""])[0][:120]
                    form    = src.get("form_type","")
                    date    = src.get("file_date","")
                    company = src.get("entity_name","")
                    url     = src.get("file_url","https://www.sec.gov")
                    if title or company:
                        sources.append(Source(type="academic",
                            title=f"SEC Filing: {company or title}",
                            url=url,
                            snippet=f"Form: {form}, Filed: {date}. {title}",
                            publisher="SEC EDGAR -- US Securities & Exchange Commission"))
            except Exception: pass

        # World Bank company/business data
        if not sources:
            r2 = await client.get("https://search.worldbank.org/api/v2/wds",
                params={"q": kw, "rows": 3, "format": "json"},
                headers={"User-Agent": "TruthScore/4.0"})
            if r2.status_code == 200:
                for doc in list(r2.json().get("documents",{}).values())[:3]:
                    if not isinstance(doc, dict): continue
                    title = doc.get("display_title","")[:120]
                    url   = doc.get("url","https://data.worldbank.org")
                    if title:
                        sources.append(Source(type="academic",
                            title=title, url=url,
                            snippet=doc.get("abstracts",{}).get("cdata!","")[:250],
                            publisher="World Bank"))
    print(f"  [BUSINESS] {len(sources)} results")
    return sources[:3]


# ── Ethics -- PhilPapers + CrossRef (free) ─────────────────────
async def search_ethics(claim: str) -> list[Source]:
    """Ethics and moral philosophy via PhilPapers and academic databases."""
    kw = extract_keywords(claim)
    if not kw: return []
    sources = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        # PhilPapers API
        r = await client.get("https://philpapers.org/api/search.json",
            params={"query": kw, "limit": 4,
                    "categories": "ethics,applied-ethics,moral-philosophy",
                    "format": "json"},
            headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code == 200:
            try:
                for paper in (r.json() if isinstance(r.json(),list) else r.json().get("papers",[]))[:4]:
                    title   = paper.get("title","")[:120]
                    authors = ", ".join(paper.get("authors",[])[:2])
                    abstract= paper.get("abstract","")[:300]
                    url     = paper.get("url","https://philpapers.org")
                    year    = paper.get("year","")
                    if title:
                        sources.append(Source(type="academic",
                            title=f"{title} ({year})",
                            url=url, snippet=abstract or f"Ethics paper by {authors}",
                            publisher=f"{authors} -- PhilPapers" if authors else "PhilPapers"))
            except Exception: pass

        # Fallback: CrossRef filtered to ethics
        if not sources:
            r2 = await client.get("https://api.crossref.org/works",
                params={"query": f"{kw} ethics morality",
                        "rows": 3,
                        "select": "title,abstract,URL,published,author,container-title",
                        "mailto": "thesis@example.com"})
            if r2.status_code == 200:
                for item in r2.json().get("message",{}).get("items",[])[:3]:
                    titles = item.get("title",[])
                    title  = titles[0][:120] if titles else ""
                    url    = item.get("URL","")
                    abstr  = re.sub(r"<[^>]+>","",item.get("abstract",""))[:250]
                    year   = item.get("published",{}).get("date-parts",[[""]])[0][0]
                    journal= (item.get("container-title") or ["Ethics Journal"])[0]
                    if title:
                        sources.append(Source(type="academic",
                            title=f"{title} ({year})",
                            url=url, snippet=abstr or f"Article in {journal}",
                            publisher=journal))
    print(f"  [ETHICS] {len(sources)} results")
    return sources[:3]


async def search_geonames(claim: str) -> list[Source]:
    """GeoNames -- 11M geographic locations with altitude, coordinates, population."""
    # Extract just the first 1-2 words (entity name, not full query)
    words = [w for w in claim.split() if len(w) > 2]
    if not words: return []
    # Try progressively: full name -> first 2 words -> first word
    candidates = []
    if len(words) >= 2: candidates.append(" ".join(words[:3]))
    candidates.append(words[0])

    user = os.getenv("GEONAMES_USER", "demo")
    sources = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for q in candidates:
            r = await client.get(
                "http://api.geonames.org/searchJSON",
                params={"q": q, "maxRows": 5, "username": user,
                        "orderby": "relevance", "style": "FULL"},
            )
            if r.status_code != 200: continue
            items = r.json().get("geonames", [])
            if not items: continue
            for item in items[:4]:
                name    = item.get("name","")
                country = item.get("countryName","")
                fclass  = item.get("fclName","") or item.get("fcodeName","")
                lat     = item.get("lat","")
                lng     = item.get("lng","")
                elev    = item.get("elevation") or item.get("srtm3","") or item.get("astergdem","")
                pop     = item.get("population",0)
                admin   = item.get("adminName1","")
                details = []
                if fclass:   details.append(fclass)
                if country:  details.append(f"Country: {country}")
                if admin and admin != name: details.append(f"Region: {admin}")
                if elev:     details.append(f"Elevation: {elev}m above sea level")
                if pop:      details.append(f"Population: {pop:,}")
                if lat and lng: details.append(f"Coordinates: {lat}°N, {lng}°E")
                if not name: continue
                sources.append(Source(
                    type="academic",
                    title=f"{name} ({country})" if country else name,
                    url=f"https://www.geonames.org/maps/google_{lat}_{lng}.html",
                    snippet=". ".join(details),
                    publisher="GeoNames Geographic Database"
                ))
            if sources: break
    print(f"  [GEONAMES] {len(sources)} results for {candidates[0]!r}")
    return sources[:3]

async def search_rest_countries(claim: str) -> list[Source]:
    """
    REST Countries API -- data about every country: capital, population,
    currency, EU membership, area, languages. No key needed.
    """
    kw = extract_keywords(claim).lower()
    if not kw: return []

    # Extract country name from claim
    stop = {"the","a","an","is","are","was","were","has","have","that","this",
            "and","or","in","of","to","for","with","joined","member","capital",
            "population","currency","country","nation","republic"}
    words = [w for w in kw.split() if len(w) > 3 and w not in stop]
    if not words: return []

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Search by name
        for word in words[:3]:
            r = await client.get(
                f"https://restcountries.com/v3.1/name/{word}",
                params={"fields": "name,capital,population,area,region,subregion,"
                                  "currencies,languages,flags,tld,cca2,eu"},
            )
            if r.status_code != 200: continue
            countries = r.json()
            if not isinstance(countries, list): continue
            sources = []
            for c in countries[:3]:
                name      = c.get("name",{}).get("common","")
                capital   = c.get("capital",[""])[0] if c.get("capital") else ""
                pop       = c.get("population",0)
                area      = c.get("area",0)
                region    = c.get("region","")
                subregion = c.get("subregion","")
                currencies = ", ".join(v.get("name","") for v in (c.get("currencies") or {}).values())
                languages  = ", ".join((c.get("languages") or {}).values())

                details = []
                if capital:    details.append(f"Capital: {capital}")
                if pop:        details.append(f"Population: {pop:,}")
                if area:       details.append(f"Area: {area:,} km²")
                if region:     details.append(f"Region: {region}")
                if currencies: details.append(f"Currency: {currencies}")
                if languages:  details.append(f"Languages: {languages}")

                if not name: continue
                sources.append(Source(
                    type="academic",
                    title=f"{name} -- Country Profile",
                    url=f"https://restcountries.com/#api-endpoints-v3-name",
                    snippet=". ".join(details),
                    publisher="REST Countries (restcountries.com)"
                ))
            if sources:
                print(f"  [COUNTRIES] {len(sources)} results")
                return sources[:2]
    return []


# ── Wikidata SPARQL -- structured geographic data ──────────────────────────
async def search_wikidata_geo(claim: str) -> list[Source]:
    """Wikidata SPARQL -- returns altitude, coordinates, area, population for geographic entities."""
    # Use just the key entity name for search, not the full query
    # Extract just named entities for Wikidata (it works best with proper nouns)
    words = [w for w in claim.split() if len(w) > 2 and w[0].isupper()]
    if not words:
        words = [w for w in claim.split() if len(w) > 3]
    # Use first 1-2 capitalized words as entity (not full query)
    search_term = words[0] if words else claim.split()[0]

    async with httpx.AsyncClient(timeout=12.0) as client:
        # Step 1: Find entity
        r = await client.get("https://www.wikidata.org/w/api.php", params={
            "action": "wbsearchentities", "search": search_term,
            "language": "en", "format": "json", "limit": 5,
        }, headers={"User-Agent": "TruthScore/4.0"})
        if r.status_code != 200: return []
        items = r.json().get("search", [])
        if not items: return []

        sources = []
        for item in items[:3]:
            qid   = item.get("id","")
            label = item.get("label","")
            desc  = item.get("description","")
            if not qid: continue

            # Step 2: Get properties
            r2 = await client.get(
                f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
            if r2.status_code != 200:
                sources.append(Source(type="wikidata", title=label,
                    url=f"https://www.wikidata.org/wiki/{qid}",
                    snippet=f"{label}: {desc}", publisher="Wikidata"))
                continue

            claims_data = r2.json().get("entities",{}).get(qid,{}).get("claims",{})
            props = []
            # P2044=elevation, P625=coords, P1082=population, P2046=area, P610=highest point
            for pid, pname in [("P2044","Elevation"),("P4552","Mountain range"),
                                ("P625","Coordinates"),("P1082","Population"),
                                ("P2046","Area km²"),("P17","Country")]:
                for snak in claims_data.get(pid,[])[:1]:
                    val = snak.get("mainsnak",{}).get("datavalue",{}).get("value",{})
                    if pid == "P2044" and isinstance(val, dict):
                        amt = str(val.get("amount","")).lstrip("+")
                        if amt: props.append(f"Elevation: {amt}m above sea level")
                    elif pid == "P625" and isinstance(val, dict):
                        lat = val.get("latitude",""); lon = val.get("longitude","")
                        if lat: props.append(f"Coordinates: {lat:.4f}°N, {lon:.4f}°E")
                    elif pid in ("P1082","P2046") and isinstance(val, dict):
                        amt = str(val.get("amount","")).lstrip("+")
                        if amt: props.append(f"{pname}: {float(amt):,.0f}")
                    elif pid == "P17" and isinstance(val, dict):
                        # Country entity ID -- just note it
                        pass

            snippet = desc
            if props: snippet = (f"{desc}. " if desc else "") + ". ".join(props)
            sources.append(Source(
                type="wikidata", title=label,
                url=f"https://www.wikidata.org/wiki/{qid}",
                snippet=snippet[:400], publisher="Wikidata Structured Data"
            ))

    print(f"  [WIKIDATA-GEO] {len(sources)} entities")
    return sources[:3]

async def search_wikidata(claim: str) -> list[Source]:
    """Wikidata structured knowledge -- curated facts, reliable."""
    kw = extract_keywords(claim)
    if not kw: return []
    headers = {"User-Agent": "TruthScore/3.0 Python/httpx"}
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
    """OpenAlex -- 250M academic papers, free."""
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
    """Google Fact Check Tools API -- professional fact-checkers."""
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


# ════════════════════════════════════════════════════════════
# GPT-4o mini REASONING  -- replaces NLI scoring
# ════════════════════════════════════════════════════════════
