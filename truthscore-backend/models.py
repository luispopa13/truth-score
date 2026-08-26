"""
TruthScore -- Pydantic Models
All request/response data models used across the pipeline.
"""
from config import *


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



class TextAnalysisResponse(BaseModel):
    """Aggregated verification for a paragraph containing multiple claims."""
    text: str
    verdict: str
    score: int
    confidence: str
    explanation: str
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
    score:          int  = 0
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


class BatchVerifyRequest(BaseModel):
    claims:  list[str]
    user_id: str = ""


class BatchVerifyResponse(BaseModel):
    results: list[VerifyResponse]
    total:   int


class CheckoutRequest(BaseModel):
    plan: str = "pro"
    success_url: str = "http://localhost:8000/app?payment=success"
    cancel_url: str = "http://localhost:8000/app"


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