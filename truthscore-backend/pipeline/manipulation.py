"""
TruthScore Manipulation Score
================================
Detects emotional/rhetorical manipulation techniques in a claim,
independent of whether the claim is true or false.

A TRUE claim can still be presented in a manipulative way (e.g. a real
statistic cherry-picked to imply a false narrative).

Techniques detected:
  - appeal_to_fear      : fear-laden language, catastrophizing
  - false_urgency       : "you MUST share this NOW", artificial time pressure
  - us_vs_them          : tribal framing, "they want to destroy us"
  - emotional_loading   : outrage bait, disgust triggers, excessive superlatives
  - cherry_picking      : isolated stat without context, ignores base rates
  - loaded_language     : terms that presuppose a conclusion ("regime", "invasion", "hoax")
  - false_consensus     : "everyone knows", "scientists all agree", "nobody believes"
  - appeal_to_authority : unchecked expert name-drop without citation
"""
from __future__ import annotations
import json as _json
import re as _re

# ── Keyword-based pre-filter (cheap, no LLM) ─────────────────────

_FEAR_PATTERNS = [
    r"\b(deadly|dangerous|catastrophic|apocalyptic|devastating|destroy|collapse|crisis|disaster|end of)\b",
    r"\b(will kill|kills people|threatens|threatens your|your life|your family)\b",
]
_URGENCY_PATTERNS = [
    r"\b(must share|share now|urgent|breaking|alert|warning|act now|before it'?s? too late)\b",
    r"\b(they'?re? hiding|censored|they don'?t want you to know)\b",
]
_US_VS_THEM_PATTERNS = [
    r"\b(elites?|globalists?|deep state|they want|they'?re? trying|the establishment|mainstream media)\b",
    r"\b(wake up|sheeple|sheep|brainwashed|indoctrinated)\b",
]
_LOADED_PATTERNS = [
    r"\b(regime|invasion|occupation|propaganda|hoax|scam|fraud|lie|fake news|corrupt|puppet)\b",
]
_FALSE_CONSENSUS_PATTERNS = [
    r"\b(everyone knows|nobody believes|all scientists|all experts|proven beyond|undeniable fact)\b",
]

_ALL_PATTERNS = {
    "appeal_to_fear": _FEAR_PATTERNS,
    "false_urgency": _URGENCY_PATTERNS,
    "us_vs_them": _US_VS_THEM_PATTERNS,
    "loaded_language": _LOADED_PATTERNS,
    "false_consensus": _FALSE_CONSENSUS_PATTERNS,
}


def _keyword_score(claim: str) -> tuple[int, list[str]]:
    lower = claim.lower()
    hits: list[str] = []
    for technique, patterns in _ALL_PATTERNS.items():
        for pat in patterns:
            if _re.search(pat, lower):
                hits.append(technique)
                break
    score = min(100, len(hits) * 20)
    return score, hits


# ── LLM-based deep analysis ───────────────────────────────────────

_PROMPT_TEMPLATE = """\
Analyze this claim for emotional and rhetorical manipulation techniques.
A claim can be TRUE but still manipulative in how it is framed.

CLAIM: {claim}

Identify which of these techniques are present (only flag if clearly present):
- appeal_to_fear: catastrophizing, fear-mongering language
- false_urgency: artificial time pressure, "share now", "before it's too late"
- us_vs_them: tribal framing, "elites", "they want to destroy us"
- emotional_loading: outrage bait, disgust triggers, excessive superlatives
- cherry_picking: isolated statistic presented without context or base rates
- loaded_language: terms that presuppose a conclusion ("regime", "invasion", "hoax")
- false_consensus: "everyone knows", "scientists all agree"
- appeal_to_authority: uncited expert name-drop used to shut down questioning

Score 0-100 where:
  0-20  = neutral factual language
  21-40 = mild rhetorical emphasis (normal in journalism)
  41-60 = moderate manipulation, some techniques present
  61-80 = significant manipulation, multiple techniques
  81-100 = highly manipulative, designed to bypass critical thinking

Respond in JSON only (no markdown):
{{"manipulation_score": <0-100>, "techniques": ["...", "..."], "summary": "<1 sentence explaining the main manipulation tactic, or 'No significant manipulation detected.' if score < 30>"}}
"""


async def score_manipulation(claim: str) -> dict:
    """
    Detect manipulation techniques in a claim.

    Returns:
      {
        "manipulation_score": int (0-100),
        "techniques": list[str],
        "summary": str,
        "is_manipulative": bool,  # score >= 50
      }
    """
    # Fast keyword pre-check
    kw_score, kw_hits = _keyword_score(claim)

    # If keyword score is high, we can skip LLM for speed
    # Otherwise run LLM for nuanced detection
    try:
        from pipeline.reasoning import call_llm_raw
        prompt = _PROMPT_TEMPLATE.format(claim=claim[:600])
        raw = await call_llm_raw(prompt, max_tokens=250, model="groq")
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if m:
            d = _json.loads(m.group(0))
            score = int(d.get("manipulation_score", kw_score))
            techniques = list(d.get("techniques") or kw_hits)
            summary = str(d.get("summary") or "")
            # Blend keyword + LLM scores
            if kw_score > 0:
                score = max(score, kw_score)
                techniques = list(set(techniques) | set(kw_hits))
            return {
                "manipulation_score": min(100, score),
                "techniques": techniques[:8],
                "summary": summary[:300],
                "is_manipulative": score >= 50,
            }
    except Exception as e:
        print(f"[manipulation] LLM error: {e}")

    # Fallback: keyword only
    summary = (
        f"Detected techniques: {', '.join(kw_hits)}." if kw_hits
        else "No significant manipulation detected."
    )
    return {
        "manipulation_score": kw_score,
        "techniques": kw_hits,
        "summary": summary,
        "is_manipulative": kw_score >= 50,
    }


def manipulation_label(score: int) -> str:
    """Human-readable label for a manipulation score."""
    if score < 20:
        return "Neutral"
    elif score < 40:
        return "Mild rhetoric"
    elif score < 60:
        return "Moderately manipulative"
    elif score < 80:
        return "Highly manipulative"
    else:
        return "Extreme manipulation"
