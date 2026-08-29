"""
TruthScore Live Debate Mode
============================
Two AI agents argue opposite sides of a claim in real-time (streamed via SSE):
  - PRO agent: builds the strongest TRUE argument with evidence
  - CON agent: builds the strongest FALSE argument with counter-evidence
  - JUDGE agent: weighs both sides and delivers a final verdict

The debate makes the reasoning process fully transparent and educational.
"""
from __future__ import annotations
import asyncio
import json


_PRO_PROMPT = """\
You are a skilled fact-checker arguing that the following claim is TRUE.
Build the strongest possible argument using specific evidence, statistics, and sources.
Be concise (3-4 sentences max). Only use real, verifiable facts.
If the claim is clearly false, still argue the strongest possible version of it.

CLAIM: {claim}

Respond in this format:
ARGUMENT: <your argument>
KEY EVIDENCE: <1-2 specific pieces of evidence with sources>
CONFIDENCE: HIGH|MEDIUM|LOW"""

_CON_PROMPT = """\
You are a skilled fact-checker arguing that the following claim is FALSE or MISLEADING.
Build the strongest possible counter-argument using specific evidence, statistics, and sources.
Be concise (3-4 sentences max). Only use real, verifiable facts.
If the claim is clearly true, still argue the strongest critique or limitation.

CLAIM: {claim}

Respond in this format:
ARGUMENT: <your counter-argument>
KEY EVIDENCE: <1-2 specific pieces of evidence with sources>
CONFIDENCE: HIGH|MEDIUM|LOW"""

_JUDGE_PROMPT = """\
You are an impartial judge evaluating a fact-checking debate.

CLAIM: {claim}

PRO ARGUMENT (arguing TRUE):
{pro_argument}

CON ARGUMENT (arguing FALSE/MISLEADING):
{con_argument}

Weigh both arguments carefully. Consider:
1. Which argument has stronger, more verifiable evidence?
2. Is the claim partially true, nuanced, or context-dependent?
3. Are there logical fallacies in either argument?

Respond in this format:
VERDICT: TRUE|FALSE|UNCERTAIN|MISLEADING
SCORE: <0-100 where 0=definitely false, 50=uncertain, 100=definitely true>
REASONING: <2-3 sentences explaining your verdict>
WINNER: PRO|CON|DRAW (whose argument was stronger)"""


async def run_debate(claim: str):
    """
    Async generator that yields debate events as JSON strings.

    Event types:
      {"type": "start", "claim": "..."}
      {"type": "pro", "text": "...", "status": "arguing"}
      {"type": "con", "text": "...", "status": "arguing"}
      {"type": "judge", "text": "...", "verdict": "TRUE|FALSE|...", "score": 70, "winner": "PRO|CON|DRAW"}
      {"type": "done"}
      {"type": "error", "message": "..."}
    """
    from pipeline.reasoning import call_llm_raw

    yield json.dumps({"type": "start", "claim": claim[:300]})

    # Run PRO and CON in parallel
    async def run_pro():
        return await call_llm_raw(_PRO_PROMPT.format(claim=claim[:400]), max_tokens=300, model="groq")

    async def run_con():
        return await call_llm_raw(_CON_PROMPT.format(claim=claim[:400]), max_tokens=300, model="gemini")

    try:
        pro_raw, con_raw = await asyncio.gather(run_pro(), run_con())
    except Exception as e:
        yield json.dumps({"type": "error", "message": str(e)})
        return

    yield json.dumps({"type": "pro", "text": pro_raw.strip(), "status": "done"})
    yield json.dumps({"type": "con", "text": con_raw.strip(), "status": "done"})

    # Judge
    try:
        judge_prompt = _JUDGE_PROMPT.format(
            claim=claim[:300],
            pro_argument=pro_raw.strip()[:600],
            con_argument=con_raw.strip()[:600],
        )
        judge_raw = await call_llm_raw(judge_prompt, max_tokens=350, model="groq")

        # Parse judge response
        import re
        verdict_m = re.search(r"VERDICT:\s*(TRUE|FALSE|UNCERTAIN|MISLEADING)", judge_raw, re.IGNORECASE)
        score_m = re.search(r"SCORE:\s*(\d+)", judge_raw)
        reasoning_m = re.search(r"REASONING:\s*(.+?)(?:WINNER:|$)", judge_raw, re.DOTALL | re.IGNORECASE)
        winner_m = re.search(r"WINNER:\s*(PRO|CON|DRAW)", judge_raw, re.IGNORECASE)

        verdict = verdict_m.group(1).upper() if verdict_m else "UNCERTAIN"
        score = int(score_m.group(1)) if score_m else 50
        reasoning = reasoning_m.group(1).strip()[:400] if reasoning_m else judge_raw.strip()[:400]
        winner = winner_m.group(1).upper() if winner_m else "DRAW"

        yield json.dumps({
            "type": "judge",
            "text": reasoning,
            "verdict": verdict,
            "score": score,
            "winner": winner,
            "raw": judge_raw.strip()[:500],
        })
    except Exception as e:
        yield json.dumps({"type": "error", "message": f"Judge failed: {e}"})

    yield json.dumps({"type": "done"})
