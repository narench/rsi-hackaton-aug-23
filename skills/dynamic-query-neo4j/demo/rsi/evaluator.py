from __future__ import annotations

import json
from pathlib import Path

from .models import BenchmarkResult, CandidateEvaluation, QueryPolicy
from .online import OnlineQueryAgent

BENCHMARK_DIR = Path(__file__).resolve().parent / "benchmarks"


def load_cases() -> list[dict]:
    cases = []
    for path in sorted(BENCHMARK_DIR.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                cases.append(json.loads(line))
    return cases


def evaluate_policy(agent: OnlineQueryAgent, policy: QueryPolicy,
                    candidate_id: str = "policy") -> CandidateEvaluation:
    results = []
    intent = policy.intents["substitution"]
    for case in load_cases():
        state = agent.run(case["question"], policy=policy, persist=False)
        answer = state["answer"]
        checks: list[tuple[str, bool, float]] = [
            ("status", answer.status == case["expected_status"], 0.30),
            ("attempts", answer.attempts <= case["max_attempts"], 0.20),
        ]
        required = set(case.get("required_policy_fields", []))
        checks.append((
            "required_identity",
            required.issubset(set(intent.required_identity)),
            0.15,
        ))
        checks.append((
            "cohort_fields",
            required.issubset(set(intent.cohort_fields)),
            0.15,
        ))
        min_a = case.get("min_a_rated")
        if min_a is not None:
            checks.append((
                "a_rated_count",
                answer.result_summary.get("a_rated_non_reference", 0) >= min_a,
                0.20,
            ))
        else:
            checks.append(("safe_clarification", answer.status == "clarification", 0.20))
        score = sum(weight for _, passed, weight in checks if passed)
        failures = [name for name, passed, _ in checks if not passed]
        results.append(BenchmarkResult(
            case_id=case["case_id"], passed=not failures,
            score=round(score, 4), failures=failures, attempts=answer.attempts,
        ))

    capability_enabled = policy.capabilities.get("biologic_interchangeability", False)
    biologic_rows = agent.executor.run("""
    MATCH (a:Application)-[:HAS_PRODUCT]->(p:BiologicProduct)
    WHERE toLower(coalesce(p.proper_name, '')) CONTAINS $ingredient
      AND toLower(coalesce(a.bla_type, '')) CONTAINS 'interchangeable'
    RETURN count(DISTINCT a) AS interchangeable_products
    """, ingredient="adalimumab")
    evidence_loaded = bool(biologic_rows and biologic_rows[0]["interchangeable_products"] > 0)
    biologic_failures = []
    if not capability_enabled:
        biologic_failures.append("capability_disabled")
    if not evidence_loaded:
        biologic_failures.append("purple_book_evidence")
    results.append(BenchmarkResult(
        case_id="adalimumab-interchangeability-gate",
        passed=not biologic_failures,
        score=1.0 if not biologic_failures else 0.0,
        failures=biologic_failures,
        attempts=1,
    ))

    total = sum(result.score for result in results) / max(len(results), 1)
    hard_failures = []
    for result in results:
        if ("status" in result.failures or "a_rated_count" in result.failures
                or result.case_id == "adalimumab-interchangeability-gate" and result.failures):
            hard_failures.append(f"{result.case_id}:" + ",".join(result.failures))
    return CandidateEvaluation(
        candidate_id=candidate_id,
        policy_version=policy.version,
        score=round(total, 4),
        passed=all(result.passed for result in results),
        regressions=0,
        hard_failures=hard_failures,
        cases=results,
    )
