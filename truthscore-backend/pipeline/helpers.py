"""
TruthScore -- Helper Functions
Topic detection, keyword extraction, claim normalization,
smart topic detection via Gemini, and utility functions.
"""
from __future__ import annotations
import unicodedata as _unicodedata
from config import *
from models import *

def _get_domain_sources():
    try:
        from pipeline.source_plan import DOMAIN_SOURCES
        return DOMAIN_SOURCES
    except ImportError:
        return {}


# ───────────────────────────────────────────────────────

def normalize_claim(text: str) -> str:
    """
    Normalize claim for cache key.
    'Vaccines cause autism?' and 'vaccines cause autism' -> same key.
    """
    t = text.lower().strip()
    t = re.sub(r"\s+", " ", t)   # collapse whitespace first
    t = t.rstrip("?!.")          # remove trailing punctuation
    t = t.strip()                # remove any trailing whitespace left after
    # Remove combining diacritics (Romanian normalization)
    t = _unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not _unicodedata.combining(c))
    return t[:200]


# ───────────────────────────────────────────────────────

def is_temporal_claim(claim: str) -> bool:
    """Detect claims requiring recent sources (news, current stats)."""
    temporal = [
        "currently", "now", "today", "latest", "recent", "this year",
        "last year", "right now", "at the moment", "as of", "in 2024",
        "in 2025", "in 2026", "acum", "actual", "recent", "curent",
        "momentan", "luna aceasta", "anul acesta",
    ]
    c = claim.lower()
    return any(w in c for w in temporal)


# ───────────────────────────────────────────────────────

def is_nuance_claim(claim: str) -> bool:
    """
    Detect claims with absolute language -- almost always FALSE in science.
    'Completely silent', 'never', 'always', 'only' = red flags.
    Triggers extra counter-evidence search.
    """
    absolutes = [
        "completely", "totally", "absolutely", "never", "always",
        "only ", "impossible", "100%", "no one", "everyone",
        "nothing ", "everything", "entirely", "solely",
        "complet", "niciodată", "întotdeauna", "doar ", "nimeni",
    ]
    c = claim.lower()
    return any(w in c for w in absolutes)


# ───────────────────────────────────────────────────────

def is_strict_domain(claim: str, topic: str) -> bool:
    """
    Medical, biological, nutritional claims need stricter evidence.
    Requires >= 2 authoritative peer-reviewed sources.
    """
    strict_topics = {
        "medical", "biology", "chemistry", "nutrition",
        "neuroscience", "climate",
    }
    strict_words = [
        "causes", "prevents", "cures", "treats", "linked to",
        "associated with", "increases risk", "reduces risk",
        "health", "cancer", "heart disease", "brain", "gene",
        "dna", "study shows", "research shows", "scientists",
        "cauzează", "previne", "tratează", "sănătate",
    ]
    if topic in strict_topics:
        return True
    c = claim.lower()
    return any(w in c for w in strict_words)


# ───────────────────────────────────────────────────────

def get_source_recency_weight(source) -> float:
    """
    Boost recent sources for medical/scientific claims.
    A 2023 meta-analysis outweighs a 2005 study.
    Returns multiplier applied to source authority score.
    """
    text = ((source.snippet or "") + " " + (source.title or "")).lower()
    years = [int(y) for y in re.findall(r"\b(20[0-2]\d)\b", text)]
    if not years:
        return 1.0
    max_year = max(years)
    if max_year >= 2022:
        return 1.35   # very recent -- strong boost
    elif max_year >= 2019:
        return 1.15   # recent -- moderate boost
    elif max_year >= 2015:
        return 1.0    # neutral
    elif max_year <= 2010:
        return 0.75   # old science -- penalize for medical claims
    return 1.0


# ───────────────────────────────────────────────────────

def build_nuance_queries(claim: str) -> list:
    """
    For absolute claims ('space is completely silent'),
    generate queries specifically targeting exceptions and nuances.
    """
    c = claim.strip().rstrip("?.")
    return [
        f"{c} exceptions nuance",
        f"{c} not entirely true",
        f"{c} actually false",
        f"is {c} completely true",
    ]


# ───────────────────────────────────────────────────────

def _strip_diacritics(text: str) -> str:
    """Remove Romanian/accented characters for better search."""
    replacements = {
        'ă':'a','â':'a','î':'i','ș':'s','ț':'t','ş':'s','ţ':'t',
        'Ă':'A','Â':'A','Î':'I','Ș':'S','Ț':'T','Ş':'S','Ţ':'T',
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


# ───────────────────────────────────────────────────────

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


# ───────────────────────────────────────────────────────

# Process-local memo for claim extraction. Re-submitting an identical
# paragraph skips its Gemini extraction call entirely (the per-claim
# verdicts then resolve through the semantic verdict cache).
_SPLIT_TTL_SECONDS = 12 * 3600
_SPLIT_CACHE_MAX = 256
_split_cache: dict = {}   # md5(text) -> (timestamp, [claims])


def _split_cache_get(key: str):
    import time
    entry = _split_cache.get(key)
    if not entry:
        return None
    ts, claims = entry
    if time.time() - ts > _SPLIT_TTL_SECONDS:
        _split_cache.pop(key, None)
        return None
    return list(claims)


def _split_cache_put(key: str, claims: list) -> None:
    if len(_split_cache) >= _SPLIT_CACHE_MAX:
        _split_cache.pop(next(iter(_split_cache)), None)
    _split_cache[key] = (__import__("time").time(), list(claims))


async def split_claims(text: str) -> list[str]:
    """Cached extraction of up to five self-contained verifiable claims.

    Two cache tiers: a cross-worker Redis layer (shared across every uvicorn
    worker / instance) in front of the process-local memo. Redis is best-effort
    — any failure silently falls back to the local cache + extraction.
    """
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return []
    import hashlib, json as _json
    key = hashlib.md5(clean.encode("utf-8", "ignore")).hexdigest()

    # Tier 1: process-local (fastest, no network)
    hit = _split_cache_get(key)
    if hit is not None:
        print("  [SPLIT] cache HIT (local) — no extraction call")
        return hit

    # Tier 2: Redis (shared across workers/instances)
    redis = None
    rkey = f"ts:split:{key}"
    try:
        from utils.redis_client import get_async_redis
        redis = get_async_redis()
        if redis:
            raw = await redis.get(rkey)
            if raw:
                claims = _json.loads(raw)
                if isinstance(claims, list):
                    _split_cache_put(key, claims)   # warm the local tier
                    print("  [SPLIT] cache HIT (redis) — no extraction call")
                    return list(claims)
    except Exception:
        redis = None

    claims = await _split_claims_uncached(clean)
    _split_cache_put(key, claims)
    if redis:
        try:
            await redis.set(rkey, _json.dumps(claims), ex=_SPLIT_TTL_SECONDS)
        except Exception:
            pass
    return claims


async def _split_claims_uncached(clean: str) -> list[str]:
    """Extract self-contained, independently verifiable claims (one fact each)."""

    # Pre-normalize: ensure a space after sentence punctuation so glued
    # sentences like "Hamlet.Apa pură" split correctly downstream.
    clean = re.sub(r"([.!?])(?=[^\s.!?;:,)\]])", r"\1 ", clean)

    # Keep atomic inputs on the fast path: no extra model call.
    sentence_marks = len(re.findall(r"[.!?]+(?:\s|$)", clean))
    has_joiner = any(j in clean.lower() for j in
                     (" și ", " and ", " iar ", " dar ", " but ", ";", ","))
    if len(clean.split()) < 12 and sentence_marks <= 1 and not has_joiner:
        return [clean]

    # Deterministic fallback if extraction is unavailable or malformed.
    parts = [s.strip(" -•\t") for s in re.split(r"(?<=[.!?])\s+|[;\n]+", clean)]
    # Contrastive conjunctions ("iar", "dar", "but") almost always join two
    # independent claims — split there too. ("și/and" is excluded on purpose:
    # compound subjects like "Mihai și Andrei" are ONE claim.)
    CONTRA = re.compile(r"\b(?:iar|dar|însă|but|however)\b", re.IGNORECASE)
    sentences: list[str] = []
    for part in parts:
        pieces = [p.strip(" ,") for p in CONTRA.split(part) if len(p.strip(" ,")) > 5]
        sentences.extend(pieces if pieces else [part])
    sentence_fallback = [s for s in sentences if len(s) > 5][:8] or [clean]
    if not gemini_client:
        return sentence_fallback

    try:
        import asyncio as _asyncio, json as _json
        prompt = f"""Extract every independently verifiable factual claim from the text below.
Return a JSON array of at most 8 strings and nothing else.
Rules:
- EXACTLY ONE fact per string. NEVER merge two facts into one string, even when both are true or both are false.
- If one sentence contains two facts joined by a connector (și, iar, dar, and, but, while), split them into separate strings.
- Sentences glued together without a space (e.g. "...Hamlet.Apa pură...") are separate claims.
- Preserve qualifiers, dates, quantities, negations and the original language.
- Resolve pronouns using context so each claim is self-contained.
- Exclude opinions, commands, questions and non-factual filler.
- Do not add facts that are not present in the text.

Example:
Text: "Parisul este capitala Franței.Everest are 8849 m iar Luna se rotește în jurul Pământului."
Output: ["Parisul este capitala Franței.", "Everest are 8849 m.", "Luna se rotește în jurul Pământului."]

TEXT:
{clean[:4000]}
"""
        loop = _asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=make_gemini_config(max_tokens=700, use_search=False, thinking_budget=0),
            ),
        )
        raw = resp.text.strip().replace("```json", "").replace("```", "").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            parsed = _json.loads(raw[start:end + 1])
            claims = []
            seen = set()
            for item in parsed if isinstance(parsed, list) else []:
                claim = " ".join(str(item).split()).strip()
                key = claim.casefold().rstrip(".!?")
                if len(claim) > 5 and key not in seen:
                    seen.add(key)
                    claims.append(claim[:1000])
            if claims:
                print(f"  [SPLIT] {len(claims)} atomic claim(s) found")
                return claims[:8]
    except Exception as exc:
        print(f"  [SPLIT] Error: {exc}")
    return sentence_fallback


# ───────────────────────────────────────────────────────

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


# ───────────────────────────────────────────────────────

def detect_topic(claim: str) -> str:
    """Comprehensive keyword-based topic detection for 27 domains."""
    c = claim.lower()
    kw = lambda *words: any(w in c for w in words)

    # ── Science & STEM ────────────────────────────────────────
    if kw("5g","conspiracy","hoax","myth","misinformation","chemtrail","flat earth",
           "microchip","vaccine","virus","cancer","disease","drug","medication","symptom",
           "covid","hiv","aids","diabetes","antibiotic","surgery","clinical",
           "hospital","patient","therapy","treatment","cure","epidemic","pandemic"):
        return "medical"
    if kw("species","evolution","dna","rna","gene","protein","cell","bacteria",
           "ecosystem","biodiversity","mammal","reptile","plant","fungus","genome",
           "photosynthesis","ecology","zoology","botany","genetics","organism"):
        return "biology"
    if kw("element","compound","molecule","reaction","acid","base","periodic",
           "oxidation","polymer","enzyme","catalyst","solvent","organic","inorganic",
           "chemistry","biochemistry","chemical","electron","bond","isotope","formula"):
        return "chemistry"
    if kw("star","nebula","telescope","galaxy","comet","asteroid","milky way",
           "exoplanet","hubble","james webb","supernova","pulsar","quasar",
           "light year","parsec","cosmology","universe expansion","dark matter",
           "astronomy","astrophysics","constellation","lunar","solar system"):
        return "astronomy"
    if kw("quantum","relativity","particle","photon","gravity","electromagnetic",
           "thermodynamics","optics","velocity","acceleration","force","entropy",
           "nuclear","radioactive","superconductor","physics","neutron","proton",
           "laser","wavelength","frequency","momentum","kinetic","potential"):
        return "physics"
    if kw("theorem","proof","equation","calculus","algebra","geometry","topology",
           "prime","fibonacci","integral","derivative","matrix","vector","polynomial",
           "statistics","probability","mathematics","math","formula","algorithm"):
        return "mathematics"
    if kw("logic","syllogism","deduction","induction","fallacy","propositional",
           "predicate","inference","valid","sound","formal logic","boolean",
           "axiom","tautology","contradiction","modus ponens","modus tollens"):
        return "logic"
    if kw("algorithm","software","programming","machine learning","artificial intelligence",
           "neural network","computer","cpu","gpu","internet","cybersecurity","blockchain",
           "database","code","compiler","cloud","api","robot","automation",
           "deep learning","nlp","data science","python","javascript","hardware"):
        return "cs_tech"
    if kw("engineering","bridge","circuit","turbine","antenna","semiconductor",
           "voltage","current","resistance","mechanical","civil","electrical",
           "structural","manufacturing","construction","hydraulic","aerospace"):
        return "engineering"
    if kw("nutrition","calorie","protein","carbohydrate","fat","vitamin","mineral",
           "diet","food","eating","nutrient","fiber","sugar","obesity","malnutrition",
           "supplement","omega","antioxidant","metabolism","caloric"):
        return "nutrition"

    # ── Humanities & Social Sciences ──────────────────────────
    if kw("philosophy","philosopher","epistemology","ontology","metaphysics",
           "phenomenology","existentialism","stoicism","utilitarianism","kantian",
           "plato","aristotle","descartes","nietzsche","socrates","hegel","locke"):
        return "philosophy"
    if kw("ethics","moral","morality","ethical","virtue","deontology","consequentialism",
           "utilitarianism","justice","fairness","rights","duty","harm","good","evil",
           "bioethics","medical ethics","ai ethics","normative"):
        return "ethics"
    if kw("religion","religious","theology","faith","scripture","god","allah","jesus",
           "buddha","muhammad","bible","quran","torah","hinduism","buddhism","islam",
           "christianity","judaism","prayer","church","mosque","temple","spiritual",
           "sacred","divine","prophets","afterlife","sin","salvation","karma"):
        return "religion"
    if kw("psychology","psychological","behavior","cognitive","emotion","mental",
           "personality","therapy","anxiety","depression","trauma","freud","jung",
           "pavlov","skinner","cognitive bias","memory","perception","motivation",
           "consciousness","subconscious","phobia","disorder","resilience"):
        return "psychology"
    if kw("sociology","social","society","culture","class","inequality","race",
           "gender","norms","institution","community","collective","urbanization",
           "migration","discrimination","prejudice","stereotype","social movement",
           "capitalism","marxism","feminism","power","privilege","identity"):
        return "sociology"
    if kw("economy","gdp","inflation","recession","stock","bitcoin","trade",
           "market","unemployment","bank","financial","currency","interest rate",
           "investment","poverty","tariff","export","import","fiscal","monetary"):
        return "economics"
    if kw("business","company","corporation","startup","revenue","profit","ceo",
           "management","strategy","marketing","brand","merger","acquisition",
           "entrepreneur","venture","ipo","stakeholder","supply chain","b2b","b2c"):
        return "business"
    if kw("war","president","election","government","parliament","senate",
           "congress","political","democrat","republican","vote","minister","policy",
           "constitution","law","court","legislation","regulation","rights","treaty"):
        return "politics"

    # ── Earth & Environment ───────────────────────────────────
    if kw("climate","temperature","carbon","co2","fossil","pollution","emission",
           "greenhouse","global warming","ozone","sea level","ice cap",
           "drought","flood","renewable","solar","wind turbine","noaa"):
        return "climate"
    if kw("mountain","peak","river","lake","ocean","country","capital","continent",
           "island","peninsula","border","territory","population","elevation",
           "altitude","european union","united nations","nato","republic","kingdom",
           "joined eu","joined the eu","joined the european","accession","membership",
           "vârf","munte","râu","geografie","capitala","județ"):
        return "geography"

    # ── Culture & Arts ────────────────────────────────────────
    if kw("born","died","founded","invented","discovered","century","ancient",
           "medieval","world war","revolution","independence","empire","colony",
           "dynasty","civilization","archaeological","roman","greek","ottoman"):
        return "history"
    if kw("novel","poem","poetry","author","writer","literature","book","play",
           "shakespeare","literary","fiction","narrative","published","manuscript",
           "dante","homer","dostoyevsky","kafka","camus","hemingway"):
        return "literature"
    if kw("painting","sculpture","architecture","museum","gallery","artist",
           "renaissance","baroque","impressionism","abstract","composer","symphony",
           "opera","theater","film","cinema","director","oscar","photography",
           "picasso","beethoven","mozart","michelangelo","dali"):
        return "art"
    if kw("football","soccer","basketball","tennis","cricket","rugby","athletics",
           "athlete","player","coach","team","match","goal","championship","league",
           "olympic","fifa","nba","nfl","wimbledon","world cup","medal","f1",
           "messi","ronaldo","lebron","federer","djokovic","verstappen"):
        return "sports"

    # ── Gossip / Celebrity / Tabloid ──────────────────────────
    # Checked before the broad "news" catch: celebrity rumor needs the gossip
    # vertical (live-web recency + honest-rumor gate), not the general news path.
    try:
        from pipeline.gossip import is_gossip_claim
        if is_gossip_claim(claim):
            return "gossip"
    except Exception:
        pass

    # ── Current Events ────────────────────────────────────────
    if kw("breaking","today","latest","yesterday","this week","announced",
           "said","according to","reported","news","current events"):
        return "news"

    return "general"


# ───────────────────────────────────────────────────────

def _safe_eval_arith(expr: str):
    """
    Evaluate a plain arithmetic expression WITHOUT eval().
    Supports + - * / // % ** and parentheses over numbers only. Any name,
    call, or attribute access raises. Exponents are capped so a claim like
    "9**9**9" can't hang the worker (the classic eval-DoS).
    """
    import ast, operator
    _OPS = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos,
    }

    def _ev(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("non-numeric constant")
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            if isinstance(node.op, ast.Pow):
                exponent = _ev(node.right)
                if abs(exponent) > 100:
                    raise ValueError("exponent too large")
                return operator.pow(_ev(node.left), exponent)
            return _OPS[type(node.op)](_ev(node.left), _ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](_ev(node.operand))
        raise ValueError("unsupported expression")

    return _ev(ast.parse(expr, mode="eval").body)


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
                actual = _safe_eval_arith(expr)
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


# ───────────────────────────────────────────────────────

def extract_keywords(text: str) -> str:
    """
    Smart keyword extraction:
    1. Multi-word named entities first (Lionel Messi, Great Wall, etc.)
    2. Single capitalized words second
    3. Important content words last
    Skips opinion words: best, worst, greatest, most, etc.
    """
    stop = {
        "the","a","an","is","are","was","were","has","have","that","this",
        "and","or","but","in","on","at","to","of","for","with","by","from",
        "it","its","not","no","does","do","did","can","could","would","will",
        "more","most","all","also","very","just","been","being","ever",
        "best","worst","greatest","worst","better","worse","good","bad",
        "world","global","international","national","official","new","old",
        "nu","ca","si","sau","dar","un","o","cel","cea","lui","este","sunt",
        "care","din","pentru","prin","despre","cel","mai","bun","buna",
    }
    # Extract multi-word named entities (e.g. "Lionel Messi", "Great Wall")
    named_entities = re.findall(r"\b[A-ZĂÂÎȘȚ][a-zăâîșț]+(?:\s+[A-ZĂÂÎȘȚ][a-zăâîșț]+)+\b", text)
    # Single capitalized words
    cap_words = re.findall(r"\b[A-ZĂÂÎȘȚ][a-zăâîșț]{2,}\b", text)
    # Regular content words
    reg_words = re.findall(r"\b[a-zăâîșț]{4,}\b", text)

    result = []
    seen = set()

    # Add named entities first (highest priority)
    for ne in named_entities[:3]:
        if ne.lower() not in stop:
            result.append(ne)
            for w in ne.split():
                seen.add(w.lower())

    # Add single cap words not already in entities
    for w in cap_words[:4]:
        if w.lower() not in stop and w.lower() not in seen:
            result.append(w)
            seen.add(w.lower())

    # Add content words
    for w in reg_words[:5]:
        if w.lower() not in stop and w.lower() not in seen:
            result.append(w)
            seen.add(w.lower())

    kw = " ".join(result[:8])[:150]
    if not kw:
        # Fallback: just return all non-stop words
        all_words = re.findall(r"\b[a-zA-ZăâîșțĂÂÎȘȚ]{3,}\b", text)
        kw = " ".join(w for w in all_words if w.lower() not in stop)[:150]
    return kw


# ───────────────────────────────────────────────────────

def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


# Domain buckets for source-type classification. The type drives the authority
# weight in PATH_B_WEIGHTS / SOURCE_AUTHORITY_WEIGHTS, so labelling a general
# news or web page as "factcheck" (weight 2.0) silently over-trusts it. These
# lists keep the label honest.
_FACTCHECK_DOMAINS = (
    "snopes.com", "factcheck.org", "politifact.com", "fullfact.org",
    "truthorfiction.com", "leadstories.com", "checkyourfact.com",
    "factcheckni.org", "africacheck.org", "poynter.org",
)
_ACADEMIC_DOMAINS = (
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "sciencedirect.com",
    "who.int", "cdc.gov", "nih.gov", "nature.com", "arxiv.org", "doi.org",
    "springer.com", "wiley.com", "jstor.org", "nasa.gov", "noaa.gov",
    "europepmc.org", "openalex.org", "semanticscholar.org", " scholar.google",
)
_NEWS_DOMAINS = (
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com",
    "theguardian.com", "washingtonpost.com", "wsj.com", "npr.org",
    "aljazeera.com", "bloomberg.com", "cnn.com", "abcnews.go.com",
)


def classify_source_type(url: str) -> str:
    """Map a URL's domain to a source type used for authority weighting.

    Returns one of: "factcheck" | "academic" | "wikipedia" | "news" | "web".
    Prefer this over hard-coding a type when the type is inferred from the
    retrieved domain rather than the API that produced it.
    """
    # Gossip/tabloid catalog wins first: a cancan.ro / TMZ URL must be typed
    # "tabloid" (weight 0.25) no matter what other heuristics would say, and a
    # reliable entertainment/wire outlet gets "news".
    try:
        from pipeline.gossip import classify_gossip_domain
        g = classify_gossip_domain(url)
        if g:
            return g
    except Exception:
        pass
    d = _domain(url).lower()
    if any(fc in d for fc in _FACTCHECK_DOMAINS):
        return "factcheck"
    if "wikipedia.org" in d or "wikidata.org" in d:
        return "wikipedia"
    if d.endswith(".edu") or d.endswith(".gov") or any(ac in d for ac in _ACADEMIC_DOMAINS):
        return "academic"
    if any(nw in d for nw in _NEWS_DOMAINS):
        return "news"
    return "web"


# ───────────────────────────────────────────────────────

def _reconstruct_abstract(inv_index: dict | None) -> str:
    """Reconstruct abstract from OpenAlex inverted index."""
    if not inv_index: return ""
    words = {}
    for word, positions in inv_index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[i] for i in sorted(words))[:600]


# ───────────────────────────────────────────────────────

def factcheck_rating_to_nli(rating: str) -> NLIScore:
    r = rating.lower()
    if any(x in r for x in ["true","correct","accurate","adevarat","verified"]):
        return NLIScore(entailment=0.90, neutral=0.07, contradiction=0.03, verdict="SUPPORTS")
    if any(x in r for x in ["false","incorrect","fake","fals","mislead","hoax","wrong"]):
        return NLIScore(entailment=0.03, neutral=0.07, contradiction=0.90, verdict="CONTRADICTS")
    if any(x in r for x in ["mixed","partial","mostly","partially","half"]):
        return NLIScore(entailment=0.40, neutral=0.30, contradiction=0.30, verdict="NEUTRAL")
    return NLIScore(entailment=0.20, neutral=0.60, contradiction=0.20, verdict="NEUTRAL")


# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]
# BENCHMARK EVALUATION  (Q1 Thesis)
# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]

import time as _time

class EvalRequest(BaseModel):
    dataset: str = Field("fever", description="fever | liar | climate-fever")
    n_samples: int = Field(20, ge=5, le=50)
    offset: int = Field(0, ge=0)

class EvalSample(BaseModel):
    claim: str
    ground_truth: str
    predicted: str
    score: int
    correct: bool
    evidence_count: int

class EvalMetrics(BaseModel):
    dataset: str
    n_total: int
    n_correct: int
    accuracy: float
    precision_true: float
    recall_true: float
    f1_true: float
    precision_false: float
    recall_false: float
    f1_false: float
    macro_f1: float
    confusion_matrix: dict
    samples: list[EvalSample]
    avg_latency_ms: float

FEVER_MAP   = {"SUPPORTS":"TRUE","REFUTES":"FALSE","NOT ENOUGH INFO":"UNCERTAIN"}
LIAR_MAP    = {"true":"TRUE","mostly-true":"TRUE","half-true":"UNCERTAIN",
               "barely-true":"UNCERTAIN","false":"FALSE","pants-fire":"FALSE"}
CLIMATE_MAP = {"SUPPORTS":"TRUE","REFUTES":"FALSE","NOT_ENOUGH_INFO":"UNCERTAIN"}

DATASET_CONFIGS = {
    "fever":         {"hf":"fever","cfg":"v1.0","split":"paper_test",
                      "claim":"claim","label":"label","map":FEVER_MAP},
    "liar":          {"hf":"liar","cfg":"default","split":"test",
                      "claim":"statement","label":"label","map":LIAR_MAP},
    "climate-fever": {"hf":"climate_fever","cfg":"default","split":"test",
                      "claim":"claim","label":"claim_label","map":CLIMATE_MAP},
}


# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]
# EXPLAINABILITY  -- Perturbation-based word importance (Q3)
# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]

class ExplainRequest(BaseModel):
    claim: str = Field(..., min_length=5, max_length=500)
    verdict: str = Field("UNCERTAIN")   # existing verdict for context
    top_evidence: str = Field("", max_length=800)  # best evidence snippet

class WordImportance(BaseModel):
    word: str
    importance: float      # positive = supports TRUE, negative = supports FALSE
    abs_importance: float  # magnitude only
    baseline_score: float
    perturbed_score: float

class ExplainResponse(BaseModel):
    claim: str
    words: list[WordImportance]
    baseline_score: float
    verdict: str
    model: str
    method: str = "perturbation-based (leave-one-out)"
    interpretation: str


class AIDetectRequest(BaseModel):
    text: str

class AIDetectResponse(BaseModel):
    text_preview:        str
    ai_probability:      float
    human_probability:   float
    verdict:             str
    confidence:          str
    model:               str = ""
    interpretation:      str = ""
    risk_level:          str = "LOW"
    avg_sentence_length: float = 0.0
    vocabulary_richness: float = 0.0
    num_sentences:       int = 0


# ───────────────────────────────────────────────────────

async def smart_detect_topic(claim: str) -> tuple:
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
        valid = set(_get_domain_sources().keys())
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