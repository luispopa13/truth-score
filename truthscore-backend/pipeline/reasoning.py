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
{{"verdict":"TRUE or FALSE or UNCERTAIN","score":0-100,"confidence":"HIGH or MEDIUM or LOW","explanation":"3-5 sentences. MANDATORY: cite specific sources BY NAME (e.g. 'According to PubMed [1]...', 'Britannica [2] states...', 'Reuters [3] reports...'). Include specific numbers, dates, or facts. Explain WHY the claim is true/false using direct evidence from the sources.","correct_answer":"ONLY when verdict is FALSE (or partly false): state the accurate fact in ONE clear sentence, with the specific correct value/name/date (e.g. 'Mount Everest is the highest peak in the world at 8,849 m.'). Empty string \"\" for TRUE or UNCERTAIN verdicts. Same language as the explanation.","supporting_indices":[indices of supporting sources, e.g.[1,3]],"contradicting_indices":[indices of contradicting, e.g.[2]],"neutral_indices":[neutral/partial indices],"irrelevant_indices":[completely irrelevant source indices]}}

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
            return 50, "UNCERTAIN", "LOW", "LLM unavailable.", [], [], top_evidence[:3], ""
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
            cam = re.search(r'"correct_answer"\s*:\s*"(.*?)(?:"|$)', raw, re.DOTALL)
            if cam: data["correct_answer"] = cam.group(1).replace("\\n"," ")[:300]
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
        return 50, "UNCERTAIN", "LOW", f"Eroare Gemini: {str(e)[:100]}", [], [], top_evidence[:3], ""

    verdict     = data.get("verdict", "UNCERTAIN").upper()
    score       = max(0, min(100, int(data.get("score", 50))))
    confidence  = data.get("confidence", "LOW").upper()
    explanation = data.get("explanation", "")
    # Accurate fact for FALSE claims (empty for TRUE/UNCERTAIN). Guard against the
    # model leaking a value on non-FALSE verdicts.
    correct_answer = (data.get("correct_answer") or "").strip()[:300]
    if verdict not in ("TRUE", "FALSE", "UNCERTAIN"):
        verdict = "UNCERTAIN"
    if verdict != "FALSE":
        correct_answer = ""

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

    # If still UNCERTAIN -> commit to a verdict only when the
    # score crosses a canonical decision threshold (config.py VERDICT_TRUE_AT /
    # VERDICT_FALSE_AT). A score inside the neutral band means the evidence
    # genuinely doesn't decide either way, so forcing TRUE/FALSE there
    # manufactures false confidence — honest UNCERTAIN is the better answer
    # (priority #2). Using the same thresholds as the rest of the pipeline keeps
    # the chip consistent with the score bar the user sees.
    if verdict == "UNCERTAIN":
        if score >= VERDICT_TRUE_AT:
            verdict, confidence = "TRUE", "LOW"
            explanation = f"[Low confidence] Evidence favors this claim. {explanation}"
            print(f"  [FORCE-TRUE] score={score}>={VERDICT_TRUE_AT} -> TRUE LOW")
        elif score < VERDICT_FALSE_AT:
            verdict, confidence = "FALSE", "LOW"
            explanation = f"[Low confidence] Evidence contradicts this claim. {explanation}"
            print(f"  [FORCE-FALSE] score={score}<{VERDICT_FALSE_AT} -> FALSE LOW")

    # ── Verdict/score reconciliation (single-claim path) ──────────
    # After the consensus tie-breaks above, the numeric `score` may have been
    # nudged while `verdict` kept its old label — so we can end up asserting
    # "TRUE" over a score the thresholds read as FALSE/UNCERTAIN (or vice-versa),
    # a visible correctness bug (the UI score bar contradicts the chip). The
    # score is the aggregate authority, so we derive the verdict the score
    # IMPLIES via the canonical thresholds; when the stated verdict disagrees we
    # DON'T hard-flip to the opposite claim (that risks asserting a fresh
    # falsehood) — we downgrade to an honest UNCERTAIN/LOW.
    score_implies = ("TRUE"  if score >= VERDICT_TRUE_AT else
                     "FALSE" if score <  VERDICT_FALSE_AT else "UNCERTAIN")
    if verdict in ("TRUE", "FALSE") and verdict != score_implies:
        print(f"  [RECONCILE] verdict={verdict} but score={score} "
              f"implies {score_implies} -> UNCERTAIN LOW")
        verdict, confidence = "UNCERTAIN", "LOW"

    # Ensure at least one source is shown when Gemini classified everything as
    # neither supporting nor contradicting. We still SURFACE the collected
    # neutral sources (so the UI isn't empty) but we do NOT relabel them into
    # supporting/contradicting: promoting a source Gemini judged irrelevant into
    # the "✅ Supports" column just because the verdict came out TRUE fabricates
    # agreement the source never expressed, and it inflates the relevant-evidence
    # count (n_relevant) that drives the confidence label. Honest neutral beats
    # fake support — the verdict stays low-confidence, as it should when no
    # source actually takes a side.
    if not supporting and not contradicting:
        real_neutral = [s for s in neutral
                        if s.publisher != "TruthScore Knowledge Base"
                        and s.url != "https://deepmind.google/technologies/gemini/"]
        for s in real_neutral:
            s.stance = "neutral"
        neutral = real_neutral

    return score, verdict, confidence, explanation, supporting, contradicting, neutral, correct_answer


# ── Multi-model consensus ─────────────────────────────────────────────────────

async def multi_model_consensus(
    claim: str,
    evidence_summary: str,
    models: list[str] | None = None,
) -> dict:
    """
    Run the same reasoning prompt on multiple LLMs in parallel.
    Returns a consensus result and a disagreement flag.

    Returns:
      {
        "verdict": "TRUE"|"FALSE"|"UNCERTAIN",
        "score": int (0-100),
        "models_agree": bool,
        "disagreement_note": str,
        "model_results": [{"model": str, "verdict": str, "score": int}],
      }
    """
    import asyncio as _asyncio
    import json as _json

    if models is None:
        models = ["groq", "gemini", "openai"]

    prompt = (
        f"Evaluate this claim based on the evidence provided.\n\n"
        f"CLAIM: {claim[:500]}\n\n"
        f"EVIDENCE SUMMARY:\n{evidence_summary[:1500]}\n\n"
        f"Respond in JSON only:\n"
        f'{{\"verdict\": \"TRUE\"|\"FALSE\"|\"UNCERTAIN\", \"score\": <0-100>, '
        f'\"confidence\": \"HIGH\"|\"MEDIUM\"|\"LOW\", \"reason\": \"<1 sentence>\"}}'
    )

    async def _run_one(model: str) -> dict:
        try:
            raw = await call_llm_raw(prompt, max_tokens=200, model=model)
            # Parse JSON
            import re
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                d = _json.loads(m.group(0))
                return {
                    "model": model,
                    "verdict": (d.get("verdict") or "UNCERTAIN").upper(),
                    "score": int(d.get("score", 50)),
                    "confidence": d.get("confidence", "MEDIUM"),
                    "reason": d.get("reason", ""),
                }
        except Exception as e:
            pass
        return {"model": model, "verdict": "UNCERTAIN", "score": 50, "confidence": "LOW", "reason": ""}

    results = await _asyncio.gather(*[_run_one(m) for m in models], return_exceptions=False)
    results = [r for r in results if isinstance(r, dict)]

    if not results:
        return {
            "verdict": "UNCERTAIN", "score": 50, "models_agree": False,
            "disagreement_note": "All models failed to respond.",
            "model_results": [],
        }

    # Consensus: majority vote on verdict
    from collections import Counter
    verdict_counts = Counter(r["verdict"] for r in results)
    majority_verdict = verdict_counts.most_common(1)[0][0]
    majority_count = verdict_counts.most_common(1)[0][1]
    models_agree = majority_count == len(results)

    # Average score (weighted by confidence)
    conf_weights = {"HIGH": 1.5, "MEDIUM": 1.0, "LOW": 0.5}
    weighted_sum = sum(r["score"] * conf_weights.get(r.get("confidence", "MEDIUM"), 1.0) for r in results)
    weight_total = sum(conf_weights.get(r.get("confidence", "MEDIUM"), 1.0) for r in results)
    consensus_score = int(weighted_sum / weight_total) if weight_total > 0 else 50

    # Disagreement note
    disagreement_note = ""
    if not models_agree:
        verdicts_str = ", ".join(f"{r['model'].split('/')[-1]}: {r['verdict']}" for r in results)
        disagreement_note = f"Models disagree: {verdicts_str}. Treating as UNCERTAIN."
        majority_verdict = "UNCERTAIN"
        consensus_score = 50

    return {
        "verdict": majority_verdict,
        "score": consensus_score,
        "models_agree": models_agree,
        "disagreement_note": disagreement_note,
        "model_results": [{"model": r["model"], "verdict": r["verdict"], "score": r["score"]} for r in results],
    }


# ── Adversarial mislead detection ────────────────────────────────────────────

async def detect_misleading(
    claim: str,
    verdict: str,
    score: int,
    explanation: str,
    supporting_sources: list[dict] | None = None,
) -> dict:
    """
    Check if a claim is "technically true but misleading" — true facts presented
    to imply a false conclusion through omission, framing, or context stripping.

    Returns:
      {
        "is_misleading": bool,
        "mislead_type": "omission"|"framing"|"cherry_picking"|"false_implication"|"none",
        "mislead_note": str,  # explanation of why it's misleading
        "corrected_context": str,  # what the full picture looks like
      }
    """
    import json as _json
    import re

    # Only check verdicts that are TRUE or MIXED (misleading claims are often technically true)
    if (verdict or "").upper() not in ("TRUE", "MIXED", "UNCERTAIN"):
        return {
            "is_misleading": False,
            "mislead_type": "none",
            "mislead_note": "",
            "corrected_context": "",
        }

    src_context = ""
    if supporting_sources:
        publishers = [s.get("publisher") or s.get("title", "") for s in supporting_sources[:3]]
        src_context = f"\nSupporting sources: {', '.join(p for p in publishers if p)}"

    prompt = (
        f"A fact-checking system rated this claim as {verdict} ({score}/100).\n\n"
        f"CLAIM: {claim[:400]}\n"
        f"CURRENT EXPLANATION: {explanation[:300]}{src_context}\n\n"
        f"Your task: Determine if this claim is TECHNICALLY TRUE but MISLEADING.\n"
        f"A claim is misleading if it:\n"
        f"- Uses true facts to imply a false conclusion (false implication)\n"
        f"- Omits crucial context that would change the interpretation (omission)\n"
        f"- Cherry-picks data while ignoring contradicting evidence\n"
        f"- Uses loaded framing to spin a neutral fact\n\n"
        f"Be strict: only flag if there is a clear, specific misleading element.\n"
        f"Respond in JSON only:\n"
        f'{{"is_misleading": true|false, '
        f'"mislead_type": "omission"|"framing"|"cherry_picking"|"false_implication"|"none", '
        f'"mislead_note": "<specific explanation if misleading, else empty string>", '
        f'"corrected_context": "<full picture in 1-2 sentences, else empty string>"}}'
    )

    try:
        raw = await call_llm_raw(prompt, max_tokens=300, model="groq")
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            d = _json.loads(m.group(0))
            return {
                "is_misleading": bool(d.get("is_misleading", False)),
                "mislead_type": d.get("mislead_type", "none"),
                "mislead_note": (d.get("mislead_note") or "")[:400],
                "corrected_context": (d.get("corrected_context") or "")[:400],
            }
    except Exception as e:
        print(f"[reasoning] detect_misleading error: {e}")

    return {
        "is_misleading": False,
        "mislead_type": "none",
        "mislead_note": "",
        "corrected_context": "",
    }