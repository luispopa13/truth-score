"""
TruthScore Domain Expert Routing
==================================
Detects the domain of a claim and applies specialized verification strategies:
  - Medical: PubMed, WHO, systematic reviews
  - Legal: EUR-Lex, official government sources
  - Financial: SEC EDGAR, World Bank, central bank data
  - Historical: Wikipedia, academic sources only
  - Political: official press releases, parliament records
  - Scientific: peer-reviewed journals, preprint servers

Each domain gets a custom system prompt fragment + source priority weights.
"""
from __future__ import annotations


# ── Domain definitions ────────────────────────────────────────────

DOMAINS = {
    "medical": {
        "keywords": ["vaccine", "drug", "disease", "cancer", "virus", "treatment",
                     "clinical trial", "symptom", "mortality", "covid", "infection",
                     "therapy", "dose", "medicine", "study", "health", "obesity",
                     "diabetes", "heart", "blood", "surgery", "medication", "fda",
                     "ema", "who", "cdc", "nih", "pubmed"],
        "source_priority": ["pubmed", "who.int", "cdc.gov", "nih.gov", "nejm.org",
                            "thelancet.com", "bmj.com", "nature.com", "science.org"],
        "search_suffix": "clinical evidence peer-reviewed",
        "system_hint": (
            "This is a MEDICAL claim. Prioritize peer-reviewed studies, randomized controlled "
            "trials (RCTs), systematic reviews, and meta-analyses over news articles. "
            "Cite PubMed IDs when available. Distinguish between correlation and causation. "
            "Note the strength of evidence: RCT > cohort study > case report > expert opinion."
        ),
        "boost_types": ["academic", "official"],
        "confidence_floor": 0.7,  # Medical claims need higher evidence quality
    },
    "legal": {
        "keywords": ["law", "regulation", "directive", "treaty", "court", "ruling",
                     "illegal", "legal", "banned", "prohibited", "gdpr", "constitution",
                     "amendment", "statute", "article", "clause", "rights", "penalty",
                     "eu law", "verdict", "judgment", "legislation"],
        "source_priority": ["eur-lex.europa.eu", "ec.europa.eu", "echr.coe.int",
                            "justice.gov", "supremecourt.gov", "legislation.gov.uk"],
        "search_suffix": "official legislation court ruling",
        "system_hint": (
            "This is a LEGAL claim. Prioritize official legal sources: EUR-Lex for EU law, "
            "official government portals, court judgments. Check jurisdiction carefully. "
            "Note if a law was recently amended or repealed. Distinguish between law-as-written "
            "and law-as-enforced."
        ),
        "boost_types": ["official", "factcheck"],
        "confidence_floor": 0.65,
    },
    "financial": {
        "keywords": ["stock", "gdp", "inflation", "revenue", "profit", "economy",
                     "market cap", "billion", "trillion", "unemployment", "interest rate",
                     "recession", "investment", "nasdaq", "nyse", "earnings", "sec",
                     "ipo", "dividend", "fiscal", "monetary policy", "federal reserve",
                     "european central bank"],
        "source_priority": ["sec.gov", "federalreserve.gov", "ecb.europa.eu",
                            "worldbank.org", "imf.org", "bloomberg.com", "reuters.com",
                            "ft.com", "wsj.com"],
        "search_suffix": "financial data official report",
        "system_hint": (
            "This is a FINANCIAL/ECONOMIC claim. Prioritize official statistical sources: "
            "SEC filings, central bank reports, IMF/World Bank data, national statistics bureaus. "
            "Check if figures are inflation-adjusted. Note the time period referenced. "
            "Distinguish between absolute and relative figures."
        ),
        "boost_types": ["official", "news"],
        "confidence_floor": 0.6,
    },
    "scientific": {
        "keywords": ["climate change", "global warming", "evolution", "quantum", "dna",
                     "genome", "nasa", "esa", "physics", "chemistry", "biology",
                     "experiment", "hypothesis", "theory", "research", "evidence",
                     "peer-reviewed", "published", "journal", "co2", "greenhouse",
                     "species", "fossil", "planet", "universe"],
        "source_priority": ["nature.com", "science.org", "nasa.gov", "esa.int",
                            "noaa.gov", "ipcc.ch", "pubmed.ncbi.nlm.nih.gov"],
        "search_suffix": "scientific consensus peer-reviewed evidence",
        "system_hint": (
            "This is a SCIENTIFIC claim. Check against the scientific consensus. "
            "Prioritize peer-reviewed journals, IPCC reports for climate, NASA for space. "
            "Note if the claim contradicts established scientific consensus — flag this explicitly. "
            "Distinguish between 'a study found' and 'scientists agree'."
        ),
        "boost_types": ["academic", "official"],
        "confidence_floor": 0.75,
    },
    "historical": {
        "keywords": ["history", "historical", "century", "war", "battle", "ancient",
                     "founded", "discovered", "invented", "born", "died", "year",
                     "decade", "era", "period", "empire", "dynasty", "revolution",
                     "signed", "treaty", "president", "first", "oldest", "earliest"],
        "source_priority": ["britannica.com", "history.com", "smithsonian.com",
                            "archives.gov", "europeana.eu", "wikipedia.org"],
        "search_suffix": "historical fact primary source",
        "system_hint": (
            "This is a HISTORICAL claim. Prioritize encyclopedia sources, academic histories, "
            "primary sources (official archives, contemporary accounts). Check dates carefully. "
            "Note if there is scholarly debate about the claim. Distinguish between facts and "
            "historical interpretation."
        ),
        "boost_types": ["academic", "news"],
        "confidence_floor": 0.6,
    },
    "political": {
        "keywords": ["president", "prime minister", "minister", "parliament", "congress",
                     "senate", "election", "vote", "policy", "government", "party",
                     "politician", "democrat", "republican", "conservative", "liberal",
                     "campaign", "ballot", "referendum", "approved", "passed"],
        "source_priority": ["reuters.com", "apnews.com", "bbc.com", "congress.gov",
                            "europarl.europa.eu", "whitehouse.gov"],
        "search_suffix": "official statement policy fact",
        "system_hint": (
            "This is a POLITICAL claim. Use only non-partisan sources. Prioritize official "
            "government documents, voting records, official press releases. "
            "For election claims, cite official electoral commission data. "
            "Flag partisan framing. Distinguish between a politician's statement and verified fact."
        ),
        "boost_types": ["news", "official", "factcheck"],
        "confidence_floor": 0.55,
    },
}

DEFAULT_DOMAIN = {
    "source_priority": [],
    "search_suffix": "",
    "system_hint": "",
    "boost_types": ["news", "official", "academic", "factcheck"],
    "confidence_floor": 0.5,
}


def detect_domain(claim: str) -> tuple[str, dict]:
    """
    Detect the primary domain of a claim.
    Returns (domain_name, domain_config).
    """
    lower = claim.lower()
    scores: dict[str, int] = {}
    for domain, cfg in DOMAINS.items():
        score = sum(1 for kw in cfg["keywords"] if kw in lower)
        if score:
            scores[domain] = score
    if not scores:
        return "general", DEFAULT_DOMAIN
    best = max(scores, key=lambda d: scores[d])
    return best, DOMAINS[best]


def get_system_hint(claim: str) -> str:
    """Return the domain-specific system prompt hint for a claim."""
    _, cfg = detect_domain(claim)
    return cfg.get("system_hint", "")


def get_search_suffix(claim: str) -> str:
    """Return extra search terms to append for domain-specific evidence."""
    _, cfg = detect_domain(claim)
    return cfg.get("search_suffix", "")


def get_source_priority(claim: str) -> list[str]:
    """Return preferred domains to prioritize in source ranking."""
    _, cfg = detect_domain(claim)
    return cfg.get("source_priority", [])


def get_confidence_floor(claim: str) -> float:
    """Return minimum confidence threshold for this domain's claims."""
    _, cfg = detect_domain(claim)
    return cfg.get("confidence_floor", 0.5)


def boost_domain_sources(sources: list[dict], claim: str) -> list[dict]:
    """
    Re-rank sources to prioritize domain-authoritative publishers.
    Domain-preferred sources get a score boost so they rank first.
    """
    priority = get_source_priority(claim)
    if not priority:
        return sources

    def source_rank(s: dict) -> int:
        url = (s.get("url") or "").lower()
        for i, domain in enumerate(priority):
            if domain in url:
                return i  # lower = higher priority
        return len(priority) + 100

    return sorted(sources, key=source_rank)


def annotate_domain(result: dict, claim: str) -> dict:
    """
    Add domain metadata to a verify result dict.
    Non-destructive: only adds new keys.
    """
    domain_name, cfg = detect_domain(claim)
    if domain_name != "general":
        result.setdefault("domain", domain_name)
        result.setdefault("domain_hint", cfg.get("system_hint", "")[:200])
    return result
