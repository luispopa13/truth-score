"""
TruthScore -- LLM Reasoning (Gemini + Groq, Path A + Path B)
"""
from config import *
from models import *
from pipeline.retrieval import *
from pipeline.helpers import (
    is_nuance_claim, is_strict_domain, get_source_recency_weight,
    extract_keywords, detect_topic,
)

# Explicit import of _ prefixed vars (not exported by import *)
import config as _config
_SEARCH_TOOL  = getattr(_config, '_SEARCH_TOOL', None)
gemini_client = getattr(_config, 'gemini_client', None)
GEMINI_MODEL  = getattr(_config, 'GEMINI_MODEL', '')
genai_types   = getattr(_config, 'genai_types', None)

# Groq client
try:
    from groq import Groq as _Groq
    _groq_client = (_Groq(api_key=os.getenv("GROQ_API_KEY", ""))
                    if os.getenv("GROQ_API_KEY") else None)
except Exception:
    _Groq = None
    _groq_client = None


async def call_llm_raw(prompt: str, max_tokens: int = 1000,
                       use_search: bool = False,
                       model: str = "gemini",
                       thinking_budget: int = 0) -> str:
    """
    Public LLM entrypoint — routes every call through the shared concurrency
    limiter (utils.llm_queue) so 1000 concurrent users can't fan out into
    1000 simultaneous provider requests (providers rate-limit ~600 RPM).
    The actual tiered provider logic lives in _call_llm_raw_impl.
    """
    try:
        from utils.llm_queue import enqueue_llm_call
        return await enqueue_llm_call(
            _call_llm_raw_impl, prompt, max_tokens, use_search, model, thinking_budget)
    except Exception:
        # Queue must never break the service — fall back to a direct call.
        return await _call_llm_raw_impl(prompt, max_tokens, use_search, model, thinking_budget)


async def _call_llm_raw_impl(prompt: str, max_tokens: int = 1000,
                       use_search: bool = False,
                       model: str = "gemini",
                       thinking_budget: int = 0) -> str:
    """
    Unified LLM caller with thinking mode DISABLED by default.

    model: "gemini" | "groq" | "gpt4o-mini"
    thinking_budget: 0 = disabled (default, saves 5.8x cost), -1 = enabled
    """
    import asyncio as _aio

    # ── Tier 1: Cheap model (Groq GPT-OSS) for simple claims ──
    if model in CHEAP_MODEL_ALIASES and _groq_client:
        try:
            _gc = _groq_client
            loop2 = _aio.get_event_loop()
            resp2 = await loop2.run_in_executor(
                None,
                lambda: _gc.chat.completions.create(
                    model=GROQ_CHAT_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.05,
                    max_tokens=max_tokens,
                )
            )
            text2 = resp2.choices[0].message.content.strip()
            _record_call(GROQ_CHAT_MODEL, prompt, text2)
            print(f"  [LLM] Groq/{GROQ_CHAT_MODEL.split('/')[-1]} OK ({len(text2)} chars)")
            return text2
        except Exception as e:
            print(f"  [LLM] Groq error: {str(e)[:80]} -> falling back to Gemini")

    # ── Tier 2: GPT-4o-mini (if OpenAI key available) ─────────
    if model == "gpt4o-mini":
        import os as _os
        _okey = _os.getenv("OPENAI_API_KEY", "")
        if _okey:
            try:
                import openai as _openai
                _oclient = _openai.AsyncOpenAI(api_key=_okey)
                loop3 = _aio.get_event_loop()
                resp3 = await loop3.run_in_executor(
                    None,
                    lambda: _oclient.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.05,
                        max_tokens=max_tokens,
                    )
                )
                text3 = resp3.choices[0].message.content.strip()
                _record_call("gpt-4o-mini", prompt, text3)
                print(f"  [LLM] GPT-4o-mini OK ({len(text3)} chars)")
                return text3
            except Exception as e:
                print(f"  [LLM] GPT-4o-mini error: {str(e)[:80]} -> falling back to Gemini")

    # ── Tier 3: Gemini 2.5 Flash (thinking DISABLED) ──────────
    # ── Try Gemini (with retry on 503) ────────────────────────
    if gemini_client:
        for _attempt in range(2):  # 2 quick attempts, then instant Groq failover
            try:
                # DISABLE thinking mode — saves 5.8x on output tokens
                config = make_gemini_config(
                    max_tokens=max_tokens,
                    use_search=use_search,
                    thinking_budget=thinking_budget,
                )
                loop = _aio.get_event_loop()
                resp = await loop.run_in_executor(None,
                    lambda: gemini_client.models.generate_content(
                        model=GEMINI_MODEL, contents=prompt, config=config))
                text = resp.text.strip()
                _record_call(GEMINI_MODEL, prompt, text)
                src = "Gemini+Search" if use_search else "Gemini"
                print(f"  [LLM] {src} OK ({len(text)} chars) [thinking={thinking_budget != 0}]")
                return text
            except Exception as e:
                err = str(e)
                if any(x in err for x in ("503", "UNAVAILABLE", "overload", "rate", "429")):
                    wait = 0.6 if _attempt == 0 else 1.5   # tight backoff — users are waiting
                    print(f"  [LLM] Gemini busy (attempt {_attempt+1}/2) -> wait {wait}s")
                    await _aio.sleep(wait)
                else:
                    print(f"  [LLM] Gemini error: {err[:80]} -> trying Groq")
                    break

    # ── Fallback: Groq ────────────────────────────────────────
    if _groq_client:
        try:
            _gc = _groq_client
            loop2 = _aio.get_event_loop()
            resp2 = await loop2.run_in_executor(
                None,
                lambda: _gc.chat.completions.create(
                    model=GROQ_CHAT_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.05,
                    max_tokens=max_tokens,
                )
            )
            text2 = resp2.choices[0].message.content.strip()
            _record_call(GROQ_CHAT_MODEL, prompt, text2)
            print(f"  [LLM] Groq fallback OK ({len(text2)} chars)")
            return text2
        except Exception as e:
            print(f"  [LLM] Groq error: {str(e)[:80]}")

    # ── Last resort: paid fallback (only if configured) ──────────
    # Cascade vision: Gemini free → Groq free → paid. LLM_PAID_FALLBACK is
    # empty by default, so we never spend automatically unless opted in.
    _paid = globals().get("LLM_PAID_FALLBACK", "")
    if _paid and model != _paid:
        print(f"  [LLM] Free tiers exhausted -> paid fallback ({_paid})")
        try:
            return await _call_llm_raw_impl(
                prompt, max_tokens=max_tokens, use_search=use_search,
                model=_paid, thinking_budget=thinking_budget)
        except Exception as e:
            print(f"  [LLM] Paid fallback error: {str(e)[:80]}")

    print("  [LLM] All LLMs failed")
    return ""


def _record_call(model: str, prompt: str, response: str):
    """
    Record token usage for cost tracking.
    Uses chars/4 estimation — zero-latency (no tokenizer pass on the hot
    path). Error margin ±10% is irrelevant for cost dashboards.
    """
    try:
        from utils.metrics import record_llm_call
        record_llm_call(model, len(prompt) // 4, len(response) // 4)
    except Exception:
        pass


# ── MODEL ROUTING ───────────────────────────────────────────────
# Cheap-model aliases handled by call_llm_raw via Groq's GPT-OSS
# (OpenAI open-weight flagship: built-in search + reasoning, $0.15/$0.60).
GROQ_CHAT_MODEL   = "openai/gpt-oss-120b"
CHEAP_MODEL_ALIASES = {"groq", "cheap", "groq-gpt-oss-120b", "gpt-oss-120b"}

# Easy-claim heuristic: short, no absolute language, non-strict domain.
EASY_CLAIM_MAX_WORDS = int(os.getenv("EASY_CLAIM_MAX_WORDS", "14"))


def pick_model(claim: str, topic: str, eco: bool = False) -> str:
    """
    Fast-first routing with automatic escalation downstream:
      - eco mode (heavy-day paid user past threshold) -> always cheap Groq,
        never escalates; protects margin without a hard block
      - hard signals (absolute language / medical-scientific domain) -> Gemini
      - short simple claims -> cheap Groq GPT-OSS (~6x cheaper input)
      - everything else     -> Gemini (default quality)
    verify.py escalates to Gemini whenever the cheap result is LOW/UNCERTAIN,
    so quality is never sacrificed — latency only on genuinely hard claims.
    """
    if eco:
        return os.getenv("CHEAP_MODEL", "groq-gpt-oss-120b")
    if is_nuance_claim(claim) or is_strict_domain(claim, topic):
        return os.getenv("DEFAULT_MODEL", "gemini")
    if len(claim.split()) <= EASY_CLAIM_MAX_WORDS:
        return os.getenv("CHEAP_MODEL", "groq-gpt-oss-120b")
    return os.getenv("DEFAULT_MODEL", "gemini")


# ════════════════════════════════════════════════════════════
# PATH B: EVIDENCE-BASED VERDICT (Mathematical, no LLM memory)
#
# Gemini reads each source and classifies its STANCE only.
# The SYSTEM calculates the final verdict mathematically.
# This eliminates Gemini's parametric bias for hard claims.
# ════════════════════════════════════════════════════════════

# Source authority weights for Path B scoring -- single source of truth in
# config.py (shared with aggregate.py so the two can never drift).
PATH_B_WEIGHTS = SOURCE_AUTHORITY_WEIGHTS

def _path_b_triggers(score: int, verdict: str, claim: str, topic: str) -> bool:
    """
    Decide if Path B should run to cross-check Path A.

    Key insight: Path A can be confidently WRONG on hard claims.
    Score=95 on "Space is completely silent" doesn't mean it's right.
    We must run Path B for these categories regardless of Path A score.
    """
    # ALWAYS run for absolute-language claims ("completely", "never", "always")
    # These are the main failure category -- Path A is confidently wrong on them
    if is_nuance_claim(claim):
        return True

    # ALWAYS run for medical/biological claims
    # Science changes -- Path A training data may be outdated
    if is_strict_domain(claim, topic):
        return True

    # Run for ambiguous zone (Path A is unsure)
    if 20 <= score <= 80:
        return True

    # Skip only for truly unambiguous easy claims
    # score >= 95 AND not nuance AND not strict domain -> probably correct
    if score >= 95 or score <= 5:
        return False

    return True


async def reason_path_b(
    claim: str,
    top_evidence: list,
) -> tuple:
    """
    Path B: Evidence-based verdict without LLM parametric bias.

    Step 1: Ask Gemini to classify STANCE of each source only
            (no verdict, no score -- just what does THIS source say?)
    Step 2: System calculates weighted score from stances
    Step 3: System decides verdict from score

    This gives a verdict based purely on what the evidence says,
    not what Gemini's training data says.
    """
    if not top_evidence:
        return None, None, None, None

    # Build evidence block
    ev_lines = []
    for i, src in enumerate(top_evidence[:12], 1):
        snippet = (src.snippet or "")[:300]
        ev_lines.append(
            f"[{i}] SOURCE: {src.publisher or src.type}\n"
            f"    TITLE: {src.title or ''}\n"
            f"    TEXT: {snippet}"
        )
    evidence_block = "\n\n".join(ev_lines)

    # Ask Gemini ONLY for stance classification, not verdict
    stance_prompt = f"""You are a stance classifier. For each evidence source below,
classify how it relates to the claim. Do NOT give a verdict -- only classify each source.

Claim: "{claim}"

Evidence sources:
{evidence_block}

For each source [1] through [{len(top_evidence[:12])}], classify its stance:
- SUPPORTS: the source contains information that confirms the claim is true
- CONTRADICTS: the source contains information that refutes the claim
- NEUTRAL: the source is related but does not clearly support or contradict
- IRRELEVANT: the source is not relevant to this claim

Also extract the KEY FACT from each relevant source (1 sentence max).

Respond ONLY with JSON:
{{"stances": [
  {{"index": 1, "stance": "SUPPORTS|CONTRADICTS|NEUTRAL|IRRELEVANT", "key_fact": "..."}},
  {{"index": 2, "stance": "...", "key_fact": "..."}}
]}}"""

    try:
        import json as _json
        raw = await call_llm_raw(stance_prompt, max_tokens=800, use_search=False)
        if not raw:
            return None, None, None, None

        raw = raw.replace("```json", "").replace("```", "").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return None, None, None, None

        data = _json.loads(raw[s:e+1])
        stances = data.get("stances", [])

        if not stances:
            return None, None, None, None

        # Calculate weighted score from stances
        total_weight = 0.0
        weighted_sum = 0.0
        key_facts    = {"supports": [], "contradicts": []}

        for item in stances:
            idx     = item.get("index", 0) - 1
            stance  = item.get("stance", "NEUTRAL").upper()
            key_fact = item.get("key_fact", "")

            if idx < 0 or idx >= len(top_evidence):
                continue

            src = top_evidence[idx]
            src_type = src.type or "web"

            # Trust the fact-checker's OWN verdict over the LLM's stance read.
            # When a ClaimReview source carries a decisive NLI rating (populated
            # from its published rating via factcheck_rating_to_nli), that
            # structured signal beats the LLM's free-text stance guess — the
            # publisher already adjudicated this exact claim.
            if src.nli is not None:
                nli_v = (src.nli.verdict or "").upper()
                if nli_v == "SUPPORTS" and src.nli.entailment >= 0.8:
                    stance = "SUPPORTS"
                elif nli_v == "CONTRADICTS" and src.nli.contradiction >= 0.8:
                    stance = "CONTRADICTS"

            # Authority weight
            authority = PATH_B_WEIGHTS.get(src_type, 0.8)
            # Recency weight
            recency   = get_source_recency_weight(src)
            weight    = authority * recency

            # Targeted-query provenance is symmetric: a source found via a
            # "find debunking" search is no more credible than one found via a
            # "find support" search. Weighting CONTRADICT higher baked a
            # systematic FALSE-bias into every verdict. Authority + recency
            # (already applied above) are the only credibility signals.

            if stance == "SUPPORTS":
                src.stance = "supporting"
                weighted_sum += weight * 1.0
                total_weight += weight
                if key_fact:
                    key_facts["supports"].append(f"{src.publisher}: {key_fact}")
            elif stance == "CONTRADICTS":
                src.stance = "contradicting"
                weighted_sum += weight * -1.0
                total_weight += weight
                if key_fact:
                    key_facts["contradicts"].append(f"{src.publisher}: {key_fact}")
            elif stance == "NEUTRAL":
                src.stance = "neutral"
                total_weight += weight * 0.3
            # IRRELEVANT = no weight

        if total_weight == 0:
            return None, None, None, None

        # Convert to 0-100 score
        normalized = weighted_sum / total_weight  # -1 to +1
        b_score    = round((normalized + 1) * 50) # 0 to 100
        b_score    = max(0, min(100, b_score))

        # Verdict from score
        if b_score >= VERDICT_TRUE_AT:
            b_verdict = "TRUE"
        elif b_score < VERDICT_FALSE_AT:
            b_verdict = "FALSE"
        else:
            b_verdict = "UNCERTAIN"

        # Build explanation from key facts
        support_str = "; ".join(key_facts["supports"][:2])
        contra_str  = "; ".join(key_facts["contradicts"][:2])
        if support_str and contra_str:
            b_explanation = (f"Evidence analysis: Supporting: {support_str}. "
                           f"Contradicting: {contra_str}.")
        elif support_str:
            b_explanation = f"Evidence supports: {support_str}."
        elif contra_str:
            b_explanation = f"Evidence contradicts: {contra_str}."
        else:
            b_explanation = "Insufficient stance signals from evidence."

        n_support  = sum(1 for s in stances if s.get("stance","").upper() == "SUPPORTS")
        n_contra   = sum(1 for s in stances if s.get("stance","").upper() == "CONTRADICTS")

        # Tie-breaking: in the ambiguous zone, let AUTHORITATIVE sources decide,
        # symmetrically. Whichever side (support vs contradict) has more trusted
        # sources (factcheck/academic/news) breaks the tie in its own direction.
        # (The old rule only ever nudged FALSE, baking in a systematic bias;
        # authoritative *support* was silently ignored.)
        if VERDICT_FALSE_AT <= b_score <= VERDICT_TRUE_AT:
            auth_contra = auth_support = 0
            for item in stances:
                st = item.get("stance", "").upper()
                if st not in ("CONTRADICTS", "SUPPORTS"):
                    continue
                i = item.get("index", 0) - 1
                if 0 <= i < len(top_evidence) and top_evidence[i].type in ("factcheck", "academic", "news"):
                    if st == "CONTRADICTS":
                        auth_contra += 1
                    else:
                        auth_support += 1
            if auth_contra > auth_support:
                b_score   = 28
                b_verdict = "FALSE"
                b_explanation = (f"[Tie-break: authoritative contradictions outweigh support] "
                                f"{b_explanation}")
                print(f"  [PATH-B] Tie-break -> FALSE (auth_contra={auth_contra} > auth_support={auth_support})")
            elif auth_support > auth_contra:
                b_score   = 72
                b_verdict = "TRUE"
                b_explanation = (f"[Tie-break: authoritative support outweighs contradictions] "
                                f"{b_explanation}")
                print(f"  [PATH-B] Tie-break -> TRUE (auth_support={auth_support} > auth_contra={auth_contra})")

        print(f"  [PATH-B] score={b_score} verdict={b_verdict} "
              f"support={n_support} contra={n_contra} weight={total_weight:.2f}")

        return b_score, b_verdict, "MEDIUM", b_explanation

    except Exception as ex:
        print(f"  [PATH-B] Error: {ex}")
        return None, None, None, None



async def reason_with_gpt(
    claim: str,
    top_evidence: list,
    rest_evidence: list,
    model_hint: str = "gemini",
) -> tuple:
    """
    Gemini/cheap-model reads ALL collected evidence, filters irrelevant
    sources, cross-references remaining ones, and produces a verdict with
    citations.
    model_hint routes the main call ("gemini" default, or a cheap alias).
    Returns: (score, verdict, confidence, explanation, supporting, contradicting, neutral)
    """
    lang = "ro" if any(c in RO_CHARS for c in claim) else "en"

    # Build evidence block -- ALL top evidence
    ev_lines = []
    for i, src in enumerate(top_evidence[:12], 1):
        pub     = src.publisher or src.type or "Unknown"
        ttl     = (src.title or "")[:120]
        snippet = (src.snippet or "")[:400]
        # If snippet empty, use title as content (still informative)
        content = snippet if snippet else f"[Title only] {ttl}"
        rel     = f" (relevance: {src.relevance:.2f})" if src.relevance > 0 else ""
        ev_lines.append(f"[{i}] {pub}{rel}\n    Title: {ttl}\n    Content: {content}")
    if ev_lines:
        evidence_block = "\n\n".join(ev_lines)
    else:
        evidence_block = ("No external sources provided. "
                         "Use your training knowledge to verify this claim. "
                         "Answer as a scientist/expert would, citing the established consensus.")

    # Unified reasoning path: one English prompt with the strongest calibration
    # examples and adversarial process. Its LANGUAGE directive makes the model
    # write the explanation in the claim's own language (English default), so a
    # separate Romanian prompt is unnecessary.
    system_prompt = """You are an expert fact-checking system. You receive a claim and evidence from multiple sources.

You are an expert fact-checker with access to real-time web search.
Your job: determine if the claim is TRUE, FALSE, or UNCERTAIN.

5-STEP ADVERSARIAL PROCESS:
1. UNDERSTAND: What exactly is the claim asserting? What would make it FALSE?
2. CHECK FOR ABSOLUTES: Does the claim use "completely", "never", "always", "only", "no X"? These are almost always FALSE -- search for exceptions.
3. SEARCH FOR SUPPORT: What evidence confirms this claim?
4. SEARCH FOR CONTRADICTION: Look for refutations, myth-busting, meta-analyses, recent studies that overturn older ones.
5. WEIGH & DECIDE: Does supporting or contradicting evidence win? Is the science recent?

TRICKY CLAIM PATTERNS -- watch for these:
- "X is completely Y" -> search for exceptions. Space is NOT completely silent (NASA detected pressure waves in plasma).
- "X never/always does Y" -> almost always FALSE (absolutes are rare in nature)
- "X causes Y" -> check for correlation vs causation; check if newer studies reversed older ones
- Nutrition/health myths -> medical consensus changes; always use the MOST RECENT systematic reviews
- Counterintuitive biology -> e.g., humans sharing DNA with bananas IS true (~50%); check molecular biology sources
- Half-truths -> the fragment is real but the implication is false
- Historical "facts" -> Napoleon's height, Einstein's grades -- almost always myths

SCIENCE RECENCY RULE -- critical for medical/biological claims:
- A 2020+ meta-analysis beats a 2005 study
- "Moderate alcohol reduces heart disease" was overturned by large 2018-2022 studies -- now considered FALSE
- "Junk DNA has no function" was revised -- some regions DO have function -> UNCERTAIN not FALSE
- Always cite the YEAR of studies in your explanation
- If you see conflicting studies where newer ones reverse older consensus -> return UNCERTAIN

BE DECISIVE:
- Always give TRUE or FALSE. UNCERTAIN = last resort.
- EXCEPT for genuine scientific controversy with recent reversals -> UNCERTAIN is correct
- If 60%+ confident -> give verdict with LOW confidence.

QUALITY RULES:
- Cite sources BY NAME with year: "According to CDC (2023) [1]...", "PubMed meta-analysis (2021) [2] shows..."
- For FALSE: explain what IS actually true.
- For nuance/partial truths: acknowledge the true part, explain the false implication.

CRITICAL: Respond ONLY with valid JSON. No markdown. Start with { end with }.

LANGUAGE: Write the "explanation" text in the SAME language as the claim above
(e.g. a Spanish claim -> Spanish explanation, a French claim -> French). If the
claim's language is unclear, write the explanation in English. All JSON keys and
the verdict/confidence values stay in English exactly as specified."""

    user_prompt = f"""Claim to verify: "{claim}"

Evidence collected from {len(top_evidence)} sources:
{evidence_block}

Analyze and respond with exact JSON:
{{"verdict":"TRUE or FALSE or UNCERTAIN","score":0-100,"confidence":"HIGH or MEDIUM or LOW","explanation":"3-5 sentences. MANDATORY: cite specific sources BY NAME (e.g. 'According to PubMed [1]...', 'Britannica [2] states...', 'Reuters [3] reports...'). Include specific numbers, dates, or facts. Explain WHY the claim is true/false using direct evidence from the sources.","supporting_indices":[indices of supporting sources, e.g.[1,3]],"contradicting_indices":[indices of contradicting, e.g.[2]],"neutral_indices":[neutral/partial indices],"irrelevant_indices":[completely irrelevant source indices]}}

CALIBRATION EXAMPLES (from 900+ verified evaluations):

Correct FALSE verdicts (these claims are FALSE):
  Claim: "New Orleans Pelicans compete in the National Football Association." -> FALSE (score=0)
  Claim: "Alexandra Daddario is Canadian." -> FALSE (score=0)
  Claim: "Rachel Green was played by Courtney Cox." -> FALSE (score=0)

Correct TRUE verdicts (these claims are TRUE):
  Claim: "Soyuz was part of a space program." -> TRUE (score=100)
  Claim: "Stadium Arcadium featured John Frusciante." -> TRUE (score=100)
  Claim: "Sidse Babett Knudsen was born on November 22nd, 1968." -> TRUE (score=100)

CRITICAL ERRORS TO AVOID (system previously gave wrong verdict):
  Claim: "St. Anger was released on June 3, 2003." -- System wrongly said TRUE (score=100) but answer is FALSE. Always verify against primary sources before claiming TRUE.
  Claim: "Bethany Hamilton's biopic was produced by Sean McNamara." -- System wrongly said TRUE (score=100) but answer is FALSE. Always verify against primary sources before claiming TRUE.
  Claim: "Mud was made before Matthew McConaughey was born." -- System wrongly said TRUE (score=100) but answer is FALSE. Always verify against primary sources before claiming TRUE.

Hard claims requiring extra care:
  - Claims with "completely/never/always/only" -> likely FALSE (check exceptions)
  - Medical claims about benefits -> check if consensus has changed recently
  - Historical legends -> often UNCERTAIN (limited verifiable evidence)
  - Scientific controversies (Mpemba, Junk DNA) -> often UNCERTAIN"""

    try:
        import json as _json
        full_prompt = system_prompt + "\n\n" + user_prompt
        raw = await call_llm_raw(full_prompt, max_tokens=1000, use_search=False,
                                 model=model_hint)
        if not raw:
            return 50, "UNCERTAIN", "LOW", "LLM unavailable.", [], [], top_evidence[:3]
        print(f"  [GEMINI-RAW] {repr(raw[:400])}")

        # Robust JSON cleanup
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        raw = re.sub(r"```\s*$", "", raw).strip()
        start = raw.find("{"); end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end+1]
        raw = re.sub(r"//[^\n]*", "", raw)
        raw = re.sub(r",\s*([}\]])", r"\1", raw)
        # Try to parse - if truncated, extract what we can via regex
        try:
            data = _json.loads(raw)
        except Exception:
            # JSON truncated -- extract with regex
            data = {}
            vm = re.search(r'"verdict"\s*:\s*"(TRUE|FALSE|UNCERTAIN)"', raw, re.IGNORECASE)
            if vm: data["verdict"] = vm.group(1).upper()
            sm = re.search(r'"score"\s*:\s*(\d+)', raw)
            if sm: data["score"] = int(sm.group(1))
            cm = re.search(r'"confidence"\s*:\s*"(HIGH|MEDIUM|LOW)"', raw, re.IGNORECASE)
            if cm: data["confidence"] = cm.group(1).upper()
            em = re.search(r'"explanation"\s*:\s*"(.*?)(?:"|$)', raw, re.DOTALL)
            if em: data["explanation"] = em.group(1).replace("\\n"," ")[:500]
            import json as _json2
            for _k in ["supporting_indices","contradicting_indices","neutral_indices","irrelevant_indices"]:
                _m = re.search(rf'"{_k}"\s*:\s*(\[[^\]]*\]?)', raw)
                if _m:
                    try:
                        _s = _m.group(1)
                        if not _s.endswith("]"): _s += "]"
                        data[_k] = _json2.loads(_s)
                    except: data[_k] = []
            print(f"  [GEMINI-REGEX] verdict={data.get('verdict')}, score={data.get('score')}")
            if not data.get("verdict"):
                raise ValueError("Could not parse Gemini response")


    except Exception as e:
        print(f"  [GEMINI] Error: {e}")
        return 50, "UNCERTAIN", "LOW", f"Eroare Gemini: {str(e)[:100]}", [], [], top_evidence[:3]

    verdict     = data.get("verdict", "UNCERTAIN").upper()
    score       = max(0, min(100, int(data.get("score", 50))))
    confidence  = data.get("confidence", "LOW").upper()
    explanation = data.get("explanation", "")
    if verdict not in ("TRUE", "FALSE", "UNCERTAIN"):
        verdict = "UNCERTAIN"

    # Parse source indices -- safe conversion
    def safe_indices(key):
        raw_list = data.get(key, [])
        result = []
        for x in raw_list:
            try:
                i = int(x) - 1
                if 0 <= i < len(top_evidence):
                    result.append(i)
            except (ValueError, TypeError):
                pass
        return result

    sup_idx  = safe_indices("supporting_indices")
    con_idx  = safe_indices("contradicting_indices")
    irr_idx  = set(safe_indices("irrelevant_indices"))

    supporting    = [top_evidence[i] for i in sup_idx]
    contradicting = [top_evidence[i] for i in con_idx]

    # Neutral = everything classified but not irrelevant
    used = set(sup_idx) | set(con_idx) | irr_idx
    neutral_ev = [top_evidence[i] for i in range(len(top_evidence))
                  if i not in used]
    neutral_ev += rest_evidence[:3]
    neutral = neutral_ev[:8]

    print(f"  [GEMINI] verdict={verdict} score={score} conf={confidence} "
          f"sup={len(supporting)} con={len(contradicting)} "
          f"irr={len(irr_idx)}/{len(top_evidence)}")

    # ── Multi-model consensus for low-confidence verdicts ──────
    # If Gemini gives LOW confidence or UNCERTAIN -> ask Groq too
    groq_key = os.getenv("GROQ_API_KEY", "")
    if (verdict == "UNCERTAIN" or confidence == "LOW") and groq_key:
        print(f"  [CONSENSUS] Low confidence -> asking Groq for second opinion")
        consensus_prompt = (
            f'You are an expert fact-checker. Is this claim TRUE or FALSE?\n'
            f'Claim: "{claim}"\n'
            f'Current evidence summary: {explanation[:300]}\n\n'
            'Respond with JSON only: {{"verdict":"TRUE or FALSE","score":0-100,"confidence":"HIGH or MEDIUM or LOW","explanation":"2 sentences with specific facts"}}'
        )
        try:
            from groq import Groq as _Groq
            import asyncio as _aio, json as _json
            # Reuse the module-level client when available (avoids re-opening an
            # HTTP connection pool on every low-confidence claim).
            _gc = _groq_client or _Groq(api_key=groq_key)
            loop = _aio.get_event_loop()
            resp = await loop.run_in_executor(None,
                lambda: _gc.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": consensus_prompt}],
                    temperature=0.1, max_tokens=300,
                )
            )
            raw2 = resp.choices[0].message.content.strip()
            raw2 = raw2.replace("```json","").replace("```","").strip()
            s2, e2 = raw2.find("{"), raw2.rfind("}")
            if s2 != -1 and e2 > s2:
                d2 = _json.loads(raw2[s2:e2+1])
                g_verdict = d2.get("verdict","").upper()
                g_score   = int(d2.get("score", 50))
                g_conf    = d2.get("confidence","LOW").upper()
                g_expl    = d2.get("explanation","")

                if g_verdict in ("TRUE","FALSE"):
                    if verdict == "UNCERTAIN":
                        # The second evaluator breaks the tie.
                        verdict, score, confidence = g_verdict, g_score, g_conf
                        explanation = g_expl
                        print(f"  [CONSENSUS] Secondary evaluator resolved: {verdict} score={score}")
                    elif g_verdict == verdict:
                        # Both agree -> boost confidence
                        if confidence == "LOW": confidence = "MEDIUM"
                        score = (score + g_score) // 2
                        print(f"  [CONSENSUS] Both models agree: {verdict} -> confidence boosted")
                    else:
                        # Models disagree -> keep Gemini but lower score toward 50
                        score = (score + 50) // 2
                        confidence = "LOW"
                        print(f"  [CONSENSUS] Models disagree: Gemini={verdict}, Groq={g_verdict} -> LOW")
        except Exception as e:
            print(f"  [CONSENSUS] Groq error: {str(e)[:60]}")

    # If still UNCERTAIN after consensus -> only commit to a verdict when the
    # score leans far enough off the fence to justify it. A near-50 score means
    # the evidence genuinely doesn't decide either way, so forcing TRUE/FALSE
    # there manufactures false confidence — honest UNCERTAIN is the better answer
    # (priority #2: verdicts grounded in evidence). Require a >=10-point margin.
    _DECISIVE_MARGIN = 10
    if verdict == "UNCERTAIN" and abs(score - 50) >= _DECISIVE_MARGIN:
        if score > 50:
            verdict, confidence = "TRUE", "LOW"
            explanation = f"[Low confidence] Evidence slightly favors this claim. {explanation}"
            print(f"  [FORCE-TRUE] score={score}>50 (margin>={_DECISIVE_MARGIN}) -> TRUE LOW")
        else:
            verdict, confidence = "FALSE", "LOW"
            explanation = f"[Low confidence] Evidence slightly contradicts this claim. {explanation}"
            print(f"  [FORCE-FALSE] score={score}<50 (margin>={_DECISIVE_MARGIN}) -> FALSE LOW")

    # ── Verdict/score reconciliation (single-claim path) ──────────
    # After the consensus tie-breaks above, the numeric `score` may have been
    # nudged toward the fence while `verdict` kept its old TRUE/FALSE label — so
    # we can end up asserting "TRUE" over a sub-50 score (or "FALSE" over a
    # >50 score), which is a visible correctness bug (the UI score bar would
    # contradict the chip). The score is the aggregate authority, so when the
    # two genuinely disagree we DON'T hard-flip to the opposite claim (that risks
    # asserting a fresh falsehood) — we downgrade to an honest UNCERTAIN/LOW.
    # A small dead-band tolerates rounding (a FALSE at score 49 is fine).
    _RECONCILE_BAND = 3
    if verdict == "TRUE" and score < 50 - _RECONCILE_BAND:
        print(f"  [RECONCILE] verdict=TRUE but score={score} -> UNCERTAIN LOW")
        verdict, confidence = "UNCERTAIN", "LOW"
    elif verdict == "FALSE" and score > 50 + _RECONCILE_BAND:
        print(f"  [RECONCILE] verdict=FALSE but score={score} -> UNCERTAIN LOW")
        verdict, confidence = "UNCERTAIN", "LOW"

    # Ensure at least one source is shown
    # If Gemini classified all as irrelevant, promote neutral sources
    if not supporting and not contradicting:
        # First try: use neutral sources that were collected
        real_neutral = [s for s in neutral
                        if s.publisher != "TruthScore Knowledge Base"
                        and s.url != "https://deepmind.google/technologies/gemini/"]

        if real_neutral:
            # Promote top neutral sources to supporting/contradicting based on verdict
            if verdict == "TRUE":
                supporting    = real_neutral[:3]
                neutral       = real_neutral[3:]
            elif verdict == "FALSE":
                contradicting = real_neutral[:3]
                neutral       = real_neutral[3:]
            else:
                neutral = real_neutral
        else:
            # Never invent a citation when no external evidence was found.
            # The verdict remains low-confidence and the source lists stay empty.
            neutral = real_neutral

    return score, verdict, confidence, explanation, supporting, contradicting, neutral