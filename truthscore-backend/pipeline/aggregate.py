"""
TruthScore -- Sub-claim aggregation.

Turns FActScore atomic-fact results into structured per-sub-claim results
(each with its own score, verdict, and the sources that support/contradict it),
then combines them into a single weighted aggregate score + verdict for the
whole compound claim.

Why weighted (not a plain mean):
  A false sub-claim backed by an authoritative fact-check should drag the whole
  claim toward FALSE far harder than an uncertain sub-claim backed by a random
  blog nudges it toward TRUE. So each sub-claim's contribution is weighted by
  the authority of its best supporting/contradicting source, times a
  confidence multiplier. On top of that a hard gate forces the whole claim to
  FALSE when any single sub-claim is decisively false on authoritative
  evidence -- because a compound statement is only as true as its weakest link.
"""
from models import Source, SubClaimResult
from config import VERDICT_TRUE_AT, VERDICT_FALSE_AT, SOURCE_AUTHORITY_WEIGHTS

# Authority weight per source type + verdict thresholds are defined once in
# config.py and shared with Path B (reasoning.py) so aggregation can never
# drift from the rest of the pipeline.
_AUTHORITY = SOURCE_AUTHORITY_WEIGHTS

# Verdict thresholds -- identical to the rest of the pipeline (Path B, verify).
_TRUE_AT  = VERDICT_TRUE_AT
_FALSE_AT = VERDICT_FALSE_AT

# A sub-claim is "decisively false on authoritative evidence" when it is FALSE,
# its score is at/below the pipeline's FALSE threshold, and at least one
# contradicting source is authoritative. Aligned with VERDICT_FALSE_AT so the
# hard gate can never disagree with what _verdict_from_score calls FALSE.
_DECISIVE_FALSE_AT = _FALSE_AT
_AUTH_TYPES = ("factcheck", "academic", "news")


def _verdict_from_score(score: int) -> str:
    if score >= _TRUE_AT:
        return "TRUE"
    if score < _FALSE_AT:
        return "FALSE"
    return "UNCERTAIN"


def _confidence_from_score(score: int) -> str:
    return "HIGH" if abs(score - 50) >= 30 else "MEDIUM"


def _split_by_stance(sources: list[Source], atom_verdict: str) -> tuple:
    """
    Partition an atom's sources into supporting / contradicting / neutral.

    Prefers the per-source stance stamped by reason_path_b; falls back to the
    factcheck NLI verdict; finally falls back to the atom's overall verdict so
    a source is never silently dropped from the UI.
    """
    supporting, contradicting, neutral = [], [], []
    for s in sources:
        stance = (s.stance or "").lower()
        if not stance and s.nli is not None:
            v = (s.nli.verdict or "").upper()
            if v == "SUPPORTS":
                stance = "supporting"
            elif v == "CONTRADICTS":
                stance = "contradicting"
            elif v == "NEUTRAL":
                stance = "neutral"
        if not stance:
            # No per-source stance and no NLI verdict: we genuinely don't know
            # which way this source cuts. Attributing it to the atom's overall
            # verdict (supporting when TRUE, contradicting when FALSE) would
            # fabricate agreement it never expressed and let a pile of unresolved
            # sources inflate one side of the tally. Treat it as neutral -- shown
            # to the user as context, excluded from the support/contradict count.
            stance = "neutral"
        s.stance = stance
        if stance == "supporting":
            supporting.append(s)
        elif stance == "contradicting":
            contradicting.append(s)
        else:
            neutral.append(s)
    return supporting, contradicting, neutral


def _best_authority(sources: list[Source]) -> float:
    """Authority weight of the most authoritative source in the list."""
    if not sources:
        return _AUTHORITY["web"]
    return max(_AUTHORITY.get(s.type or "web", _AUTHORITY["web"]) for s in sources)


def sub_claim_weight(supporting: list[Source], contradicting: list[Source],
                     neutral: list[Source], verdict: str, score: int) -> float:
    """
    Weight one sub-claim's contribution to the aggregate: authority of the best
    source on the deciding side times a confidence multiplier. Shared by
    build_sub_claim_results (FActScore path) and main.py's /analyze-text loop so
    the paragraph score is a real authority-weighted aggregate, not a plain mean.
    """
    deciding = contradicting if verdict == "FALSE" else supporting
    authority = _best_authority(deciding or (supporting + contradicting + neutral))
    conf_mult = 1.3 if abs(score - 50) >= 24 else 1.0
    return round(authority * conf_mult, 3)


def build_sub_claim_results(atom_results: list[dict]) -> list[SubClaimResult]:
    """
    Convert FActScore atom dicts into SubClaimResult objects.

    Each atom dict is {"fact", "verdict", "score", "explanation", "sources"}.
    Stamps claim_index onto every source (source -> sub-claim mapping) and
    computes a per-sub-claim weight for the aggregate step.
    """
    subs: list[SubClaimResult] = []
    for i, atom in enumerate(atom_results):
        raw_sources = atom.get("sources") or []
        # Ensure they are Source models (they already are, coming from the pipeline).
        sources = [s for s in raw_sources if isinstance(s, Source)]
        verdict = atom.get("verdict") or "UNCERTAIN"
        score = int(atom.get("score", 50))

        supporting, contradicting, neutral = _split_by_stance(sources, verdict)

        # Map every source back to this sub-claim.
        for s in sources:
            s.claim_index = i

        # Weight = authority of the best source on the deciding side, times a
        # confidence multiplier (decisive verdicts count more than uncertain).
        weight = sub_claim_weight(supporting, contradicting, neutral, verdict, score)

        subs.append(SubClaimResult(
            claim_index=i,
            claim=atom.get("fact", ""),
            score=score,
            verdict=verdict,
            confidence=_confidence_from_score(score),
            explanation=atom.get("explanation") or "No detailed explanation available.",
            topic=atom.get("topic", "general"),
            supporting=supporting,
            contradicting=contradicting,
            neutral_sources=neutral,
            evidence_count=len(sources),
            weight=weight,
        ))
    return subs


def _has_decisive_false(subs: list[SubClaimResult]) -> bool:
    for s in subs:
        if s.verdict != "FALSE" or s.score > _DECISIVE_FALSE_AT:
            continue
        if any((src.type or "") in _AUTH_TYPES for src in s.contradicting):
            return True
    return False


def aggregate_score(subs: list[SubClaimResult]) -> tuple:
    """
    Combine sub-claim results into (score, verdict, confidence, reason).

    aggregate = round( sum(score_i * weight_i) / sum(weight_i) )

    Hard gate: if any sub-claim is decisively false on authoritative evidence,
    the whole compound claim is FALSE regardless of the weighted mean -- a chain
    is only as true as its weakest verified link.
    """
    if not subs:
        return 50, "UNCERTAIN", "MEDIUM", "No sub-claims to aggregate."

    total_w = sum(s.weight for s in subs) or 1.0
    weighted = sum(s.score * s.weight for s in subs) / total_w
    score = max(0, min(100, round(weighted)))

    n_true = sum(1 for s in subs if s.verdict == "TRUE")
    n_false = sum(1 for s in subs if s.verdict == "FALSE")
    n_unc = sum(1 for s in subs if s.verdict == "UNCERTAIN")
    total = len(subs)

    if _has_decisive_false(subs):
        worst = min(
            (s for s in subs if s.verdict == "FALSE"),
            key=lambda s: s.score,
        )
        # Cap strictly BELOW the FALSE threshold so the displayed score can never
        # disagree with the FALSE verdict (a score of exactly _FALSE_AT reads as
        # UNCERTAIN via _verdict_from_score).
        score = min(score, _FALSE_AT - 1)
        verdict = "FALSE"
        reason = (
            f"Overall FALSE: {n_false} of {total} sub-claims are contradicted, "
            f"including a decisive one backed by authoritative sources "
            f"(\"{worst.claim[:80]}\"). A compound statement cannot be true when "
            f"a key part of it is demonstrably false."
        )
    else:
        verdict = _verdict_from_score(score)
        # Conjunction gate: a compound claim is a logical AND of its parts, so it
        # cannot be TRUE unless EVERY sub-claim is TRUE. When a part is
        # contradicted or unresolved, the authority-weighted mean can still land
        # above the TRUE threshold (e.g. one strong TRUE atom outweighing a weak
        # FALSE one) -- which would wrongly report the whole claim TRUE. Cap the
        # score just below TRUE so the best honest verdict for a claim with a
        # false/unknown part is UNCERTAIN (or FALSE if the mean is low enough).
        if verdict == "TRUE" and n_true < total:
            score = min(score, _TRUE_AT - 1)
            verdict = _verdict_from_score(score)
        parts = []
        if n_true:
            parts.append(f"{n_true} supported")
        if n_false:
            parts.append(f"{n_false} contradicted")
        if n_unc:
            parts.append(f"{n_unc} uncertain")
        breakdown = ", ".join(parts)
        gate_note = ""
        if n_true < total:
            gate_note = (
                " Not every part is confirmed, so the claim as a whole cannot be "
                "rated TRUE."
            )
        reason = (
            f"Weighted across {total} sub-claims ({breakdown}), giving more "
            f"weight to sub-claims backed by authoritative sources. "
            f"Aggregate score {score}/100 -> {verdict}.{gate_note}"
        )

    confidence = _confidence_from_score(score)
    return score, verdict, confidence, reason
