"""
TruthScore -- Pydantic Models
All request/response data models used across the pipeline.
"""
from config import *
from pydantic import field_validator


class VerifyRequest(BaseModel):
    text: str = Field(..., min_length=5, max_length=4000)

    @field_validator("text")
    @classmethod
    def _not_only_whitespace(cls, v: str) -> str:
        # min_length counts raw chars, so "      " (6 spaces) passes the length
        # gate but is an empty claim that wastes an LLM call and returns garbage.
        # Reject anything whose trimmed form is shorter than the real minimum.
        if len(v.strip()) < 5:
            raise ValueError("Claim must contain at least 5 non-whitespace characters.")
        return v


class NLIScore(BaseModel):
    entailment:    float
    neutral:       float
    contradiction: float
    verdict:       str


class Source(BaseModel):
    type:      str
    title:     str
    url:       str
    snippet:   str   = ""
    publisher: str   = ""
    nli:       NLIScore | None = None
    relevance: float = 0.0
    # Which sub-claim this source is evidence for (-1 = not mapped / whole claim),
    # and whether it supports/contradicts/is neutral toward that sub-claim.
    # Additive with defaults so previously-cached Source blobs still deserialize.
    claim_index: int = -1
    stance:      str = ""
    # Which retrieval direction surfaced this source (support / contradict /
    # neutral / hyde). Kept OUT of `snippet` so the user-visible text and the
    # NLI/embedding input stay clean; purely provenance metadata. Additive with a
    # default so previously-cached Source blobs still deserialize.
    retrieval_hint: str = ""


class LatencyBreakdown(BaseModel):
    total_ms:           float = 0.0
    retrieval_ms:       float = 0.0
    embedding_ms:       float = 0.0
    nli_ms:             float = 0.0
    aggregation_ms:     float = 0.0
    sources_per_second: float = 0.0


class VerifyResponse(BaseModel):
    claim:                 str
    score:                 int
    verdict:               str
    confidence:            str
    explanation:           str
    topic:                 str              = "general"
    supporting:            list[Source]     = Field(default_factory=list)
    contradicting:         list[Source]     = Field(default_factory=list)
    neutral_sources:       list[Source]     = Field(default_factory=list)
    evidence_count:        int              = 0
    models_used:           list[str]        = Field(default_factory=list)
    latency:               LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    cached:                bool             = False
    sub_claims:            list[str]        = Field(default_factory=list)
    word_importance:       list[dict]       = Field(default_factory=list)
    calibrated_confidence: str              = ""
    # Per-sub-claim breakdown (empty for simple single claims). When populated,
    # `score`/`verdict` above are the weighted aggregate of these, and
    # `aggregate_reason` explains how the aggregate was reached.
    sub_claim_results:     list["SubClaimResult"] = Field(default_factory=list)
    aggregate_reason:      str              = ""
    check_count:           int              = 0
    # Moat: domain detection
    domain:                  str              = ""
    domain_hint:             str              = ""
    # Moat: multi-model consensus
    model_results:           list[dict]       = Field(default_factory=list)
    models_agree:            bool | None      = None
    disagreement_note:       str              = ""
    # Moat: mislead detection
    is_misleading:           bool             = False
    mislead_type:            str              = ""
    mislead_note:            str              = ""
    corrected_context:       str              = ""
    # Moat: manipulation score
    manipulation_score:      int              = 0
    manipulation_techniques: list[str]        = Field(default_factory=list)
    manipulation_summary:    str              = ""
    is_manipulative:         bool             = False
    # Entity memory profiles
    entity_profiles:         list[dict]       = Field(default_factory=list)



class SubClaimResult(BaseModel):
    """One decomposed sub-claim with its own score, verdict, and mapped sources."""
    claim_index:         int
    claim:               str
    score:               int
    verdict:             str
    confidence:          str
    explanation:         str          # always set, never empty
    topic:               str          = "general"
    supporting:          list[Source] = Field(default_factory=list)
    contradicting:       list[Source] = Field(default_factory=list)
    neutral_sources:     list[Source] = Field(default_factory=list)
    evidence_count:      int          = 0
    weight:              float        = 1.0


# VerifyResponse references SubClaimResult as a forward ref; resolve it now that
# SubClaimResult is defined.
VerifyResponse.model_rebuild()



class TextAnalysisResponse(BaseModel):
    """Aggregated verification for a paragraph containing multiple claims."""
    text: str
    verdict: str
    score: int
    confidence: str
    explanation: str
    aggregate_reason: str = ""
    results: list[VerifyResponse] = Field(default_factory=list)
    claim_count: int = 0
    mixed: bool = False
    # Fair-use transparency: how many daily-quota units this paragraph burned
    # (one per verified claim) and how many remain today.
    quota_consumed: int = 0
    quota_left:     int = -1
    check_count:    int = 0


class ClaimDetectRequest(BaseModel):
    text: str = Field(..., min_length=10, max_length=5000)


class DetectedClaim(BaseModel):
    claim:    str
    position: int   = 0
    score:    float = 0.0


class ClaimDetectResponse(BaseModel):
    claims: list[DetectedClaim]
    count:  int


class FeedbackRequest(BaseModel):
    # Length-bounded so feedback can't be used to flood the calibration store
    # with megabyte payloads (memory/DoS). claim mirrors /verify's 4000-char cap.
    claim:          str = Field("", max_length=4000)
    verdict:        str = Field("", max_length=24)
    # None (not 0) is the "unset" sentinel so /feedback can fall back to
    # predicted_score. A default of 0 would look like a real score of zero and
    # suppress the alias, mislabeling extension feedback as score=0.
    score:          int | None = None
    topic:          str = Field("general", max_length=40)
    correct:        bool = False
    failure_reason: str = Field("", max_length=500)
    # Backward-compatible aliases -- the browser extension (popup.js) sends
    # these names instead of verdict/score/correct. Accepting both means
    # neither the extension nor the dashboard ever 422s on this endpoint,
    # regardless of which naming convention the caller uses.
    predicted_verdict: str | None  = Field(None, max_length=24)
    predicted_score:   int | None  = None
    user_says_correct: bool | None = None
    source_page:       str         = Field("", max_length=300)
    # Links this feedback back to the exact verification it refers to (the value
    # of the X-TruthScore-Interaction-Id response header from /verify). Lets the
    # calibration loop join a correctness label to the logged interaction's real
    # score/sources/models instead of re-deriving them from the claim text.
    interaction_id:    str | None  = Field(None, max_length=64)


class BatchVerifyRequest(BaseModel):
    # Bounded so a single request can't fan out into thousands of paid LLM
    # calls (cost/DoS protection). Each claim is length-capped like /verify.
    claims:  list[str] = Field(..., min_length=1, max_length=50)
    user_id: str = ""

    @field_validator("claims")
    @classmethod
    def _bound_claim_lengths(cls, v):
        cleaned = [c.strip() for c in v if c and c.strip()]
        if not cleaned:
            raise ValueError("At least one non-empty claim is required.")
        for c in cleaned:
            if len(c) > 4000:
                raise ValueError("Each claim must be at most 4000 characters.")
        return cleaned


class BatchVerifyResponse(BaseModel):
    results: list[VerifyResponse]
    total:   int
    success: int = 0
    failed:  int = 0


class CheckoutRequest(BaseModel):
    # success_url / cancel_url are intentionally NOT accepted from the client:
    # letting the caller choose the post-checkout redirect is an open-redirect /
    # phishing vector (Stripe would happily bounce the user to any URL). They're
    # built server-side from the trusted PUBLIC_BASE_URL in create_checkout.
    plan: str = "pro"


class GoogleAuthRequest(BaseModel):
    token: str


class AIDetectRequest(BaseModel):
    text: str = Field(..., min_length=50, max_length=10000)


class AIDetectResponse(BaseModel):
    is_ai_generated: bool
    confidence:      float
    explanation:     str


class EvalRequest(BaseModel):
    claims: list[str]
    labels: list[str]


class EvalSample(BaseModel):
    claim:     str
    label:     str
    predicted: str
    score:     int
    correct:   bool


class EvalMetrics(BaseModel):
    accuracy:  float
    macro_f1:  float
    samples:   list[EvalSample] = []


class ExplainRequest(BaseModel):
    claim:   str
    verdict: str
    score:   int


class WordImportance(BaseModel):
    word:  str
    score: float


class ExplainResponse(BaseModel):
    word_importance: list[WordImportance]



