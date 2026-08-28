"""
TruthScore -- Pydantic Models
All request/response data models used across the pipeline.
"""
from config import *
from pydantic import field_validator


class VerifyRequest(BaseModel):
    text: str = Field(..., min_length=5, max_length=4000)


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
    claim:          str
    verdict:        str  = ""
    # None (not 0) is the "unset" sentinel so /feedback can fall back to
    # predicted_score. A default of 0 would look like a real score of zero and
    # suppress the alias, mislabeling extension feedback as score=0.
    score:          int | None = None
    topic:          str  = "general"
    correct:        bool = False
    failure_reason: str  = ""
    # Backward-compatible aliases -- the browser extension (popup.js) sends
    # these names instead of verdict/score/correct. Accepting both means
    # neither the extension nor the dashboard ever 422s on this endpoint,
    # regardless of which naming convention the caller uses.
    predicted_verdict: str | None  = None
    predicted_score:   int | None  = None
    user_says_correct: bool | None = None
    source_page:       str         = ""
    # Links this feedback back to the exact verification it refers to (the value
    # of the X-TruthScore-Interaction-Id response header from /verify). Lets the
    # calibration loop join a correctness label to the logged interaction's real
    # score/sources/models instead of re-deriving them from the claim text.
    interaction_id:    str | None  = None


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
    plan: str = "pro"
    success_url: str = f"{PUBLIC_BASE_URL}/app?payment=success"
    cancel_url: str = f"{PUBLIC_BASE_URL}/app"


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


class UserRegister(BaseModel):
    email:    str
    password: str
    name:     str = ""


class UserLogin(BaseModel):
    email:    str
    password: str