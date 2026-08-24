from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IntentPolicy(BaseModel):
    source: str
    required_identity: list[str]
    cohort_fields: list[str]
    validators: list[str]
    repairs: dict[str, str] = Field(default_factory=dict)


class QueryPolicy(BaseModel):
    version: int
    parent: int | None = None
    description: str = ""
    capabilities: dict[str, bool] = Field(default_factory=dict)
    intents: dict[str, IntentPolicy]


class ResolvedIdentity(BaseModel):
    term: str
    ingredient: str
    strength: str | None = None
    dosage_form_route: str | None = None
    te_code: str | None = None
    selected_product_id: str | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)


class QueryPlan(BaseModel):
    intent: str
    template_id: str
    cypher: str
    parameters: dict[str, Any]
    cohort_fields: list[str]
    expected_grain: list[str]


class ValidationResult(BaseModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    repairable: bool = False


class RepairRecord(BaseModel):
    failures: list[str]
    added_fields: list[str]
    reason: str


class AnswerEnvelope(BaseModel):
    status: Literal["answered", "clarification", "failed"]
    intent: str
    policy_version: int
    attempts: int
    resolved_entities: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    text: str


class PolicyOperation(BaseModel):
    op: Literal[
        "add_required_identity_field", "add_cohort_field", "set_repair",
        "enable_capability",
    ]
    intent: str
    field: str | None = None
    failure: str | None = None
    action: str | None = None


class PolicyPatch(BaseModel):
    candidate_id: str
    parent_version: int
    reason: str
    operations: list[PolicyOperation]


class BenchmarkResult(BaseModel):
    case_id: str
    passed: bool
    score: float
    failures: list[str] = Field(default_factory=list)
    attempts: int = 0


class CandidateEvaluation(BaseModel):
    candidate_id: str
    policy_version: int
    score: float
    passed: bool
    regressions: int
    hard_failures: list[str] = Field(default_factory=list)
    cases: list[BenchmarkResult] = Field(default_factory=list)
