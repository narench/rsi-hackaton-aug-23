from __future__ import annotations

import json
import uuid
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .executor import GraphExecutor, build_substitution_plan, validate_substitution
from .models import (
    AnswerEnvelope,
    QueryPlan,
    QueryPolicy,
    RepairRecord,
    ResolvedIdentity,
    ValidationResult,
)
from .policy import load_active_policy
from .store import EpisodeStore


class QueryState(TypedDict, total=False):
    run_id: str
    question: str
    intent: str
    policy: QueryPolicy
    term: str | None
    identity: ResolvedIdentity | None
    plan: QueryPlan | None
    rows: list[dict[str, Any]]
    validation: ValidationResult | None
    repairs: list[RepairRecord]
    attempt_traces: list[dict[str, Any]]
    attempt: int
    terminal_status: str | None
    terminal_reason: str | None
    answer: AnswerEnvelope | None


def _table(rows: list[dict], limit: int = 20) -> str:
    header = "| Product | Application | TE | RLD | Manufacturer |\n|---|---|---|---|---|"
    lines = []
    for row in rows[:limit]:
        values = [
            row.get("product") or "Not loaded",
            row.get("application") or "Not loaded",
            row.get("te_code") or "Missing",
            row.get("rld") or "Not loaded",
            row.get("manufacturer") or "Not loaded",
        ]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |")
    return header + "\n" + "\n".join(lines)


class OnlineQueryAgent:
    def __init__(self, executor: GraphExecutor | None = None,
                 store: EpisodeStore | None = None,
                 max_attempts: int = 3):
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.executor = executor or GraphExecutor()
        self.store = store or EpisodeStore()
        self.max_attempts = max_attempts
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(QueryState)
        builder.add_node("classify", self.classify)
        builder.add_node("resolve", self.resolve)
        builder.add_node("plan", self.plan)
        builder.add_node("execute", self.execute)
        builder.add_node("validate", self.validate)
        builder.add_node("repair", self.repair)
        builder.add_node("render", self.render)
        builder.add_edge(START, "classify")
        builder.add_edge("classify", "resolve")
        builder.add_edge("resolve", "plan")
        builder.add_conditional_edges("plan", self.route_after_plan, {
            "execute": "execute", "render": "render",
        })
        builder.add_edge("execute", "validate")
        builder.add_conditional_edges("validate", self.route_after_validate, {
            "repair": "repair", "render": "render",
        })
        builder.add_conditional_edges("repair", self.route_after_repair, {
            "execute": "execute", "render": "render",
        })
        builder.add_edge("render", END)
        return builder.compile(checkpointer=InMemorySaver())

    def classify(self, state: QueryState) -> dict:
        return {"intent": "substitution"}

    def resolve(self, state: QueryState) -> dict:
        term = self.executor.resolve_name(state["question"])
        if not term:
            return {
                "term": None, "identity": None,
                "terminal_status": "failed",
                "terminal_reason": "No loaded Orange Book drug name could be resolved.",
            }
        identity = self.executor.resolve_orange_identity(state["question"], term)
        if identity is None:
            return {
                "term": term, "identity": None,
                "terminal_status": "failed",
                "terminal_reason": f"{term} did not resolve to an Orange Book product.",
            }
        return {"term": term, "identity": identity}

    def plan(self, state: QueryState) -> dict:
        if state.get("terminal_status"):
            return {}
        identity = state["identity"]
        policy = state["policy"].intents["substitution"]
        missing = [field for field in policy.required_identity if getattr(identity, field) is None]
        if missing:
            strengths = sorted({x.get("strength") for x in identity.alternatives if x.get("strength")})
            return {
                "terminal_status": "clarification",
                "terminal_reason": (
                    "The active policy requires " + ", ".join(missing) + ". "
                    + ("Available strengths: " + ", ".join(strengths[:12]) if strengths else "")
                ),
            }
        try:
            plan = build_substitution_plan(identity, policy.cohort_fields)
        except ValueError as exc:
            return {"terminal_status": "failed", "terminal_reason": str(exc)}
        return {"plan": plan, "attempt": 1}

    @staticmethod
    def route_after_plan(state: QueryState) -> str:
        return "render" if state.get("terminal_status") else "execute"

    def execute(self, state: QueryState) -> dict:
        try:
            rows = self.executor.run(state["plan"].cypher, **state["plan"].parameters)
            trace = {
                "attempt": state.get("attempt", 1),
                "template_id": state["plan"].template_id,
                "cypher": state["plan"].cypher,
                "parameters": state["plan"].parameters,
                "row_count": len(rows),
            }
            return {"rows": rows, "attempt_traces": [*state.get("attempt_traces", []), trace]}
        except Exception as exc:
            return {
                "rows": [],
                "validation": ValidationResult(
                    passed=False, failures=["EXECUTION_ERROR"],
                    metrics={"error": str(exc)}, repairable=False,
                ),
                "terminal_status": "failed",
                "terminal_reason": f"Query execution failed: {exc}",
            }

    def validate(self, state: QueryState) -> dict:
        if state.get("validation") and "EXECUTION_ERROR" in state["validation"].failures:
            return {}
        validation = validate_substitution(state.get("rows", []))
        traces = list(state.get("attempt_traces", []))
        if traces:
            traces[-1] = {**traces[-1], "validation": validation.model_dump()}
        return {"validation": validation, "attempt_traces": traces}

    def route_after_validate(self, state: QueryState) -> str:
        validation = state["validation"]
        if validation.passed:
            return "render"
        if validation.repairable and state.get("attempt", 1) < self.max_attempts:
            return "repair"
        return "render"

    def repair(self, state: QueryState) -> dict:
        identity = state["identity"]
        required = ["ingredient", "strength", "dosage_form_route", "te_code"]
        missing = [field for field in required if getattr(identity, field) is None]
        if missing:
            strengths = sorted({x.get("strength") for x in identity.alternatives if x.get("strength")})
            return {
                "terminal_status": "clarification",
                "terminal_reason": (
                    "The first query mixed pharmaceutical-equivalence cohorts. "
                    "Please specify " + ", ".join(missing) + ". "
                    + ("Available strengths: " + ", ".join(strengths[:12]) if strengths else "")
                ),
            }
        repaired_plan = build_substitution_plan(identity, required)
        repair = RepairRecord(
            failures=state["validation"].failures,
            added_fields=[field for field in required if field not in state["plan"].cohort_fields],
            reason="Constrain the query to one pharmaceutical-equivalence cohort.",
        )
        return {
            "plan": repaired_plan,
            "repairs": [*state.get("repairs", []), repair],
            "attempt": state.get("attempt", 1) + 1,
            "validation": None,
            "terminal_status": None,
            "terminal_reason": None,
        }

    @staticmethod
    def route_after_repair(state: QueryState) -> str:
        return "render" if state.get("terminal_status") else "execute"

    def render(self, state: QueryState) -> dict:
        policy_version = state["policy"].version
        attempts = state.get("attempt", 0)
        if state.get("terminal_status") == "clarification":
            answer = AnswerEnvelope(
                status="clarification", intent=state["intent"], policy_version=policy_version,
                attempts=attempts, text=state.get("terminal_reason") or "More information is required.",
                limitations=["No substitution conclusion was produced."],
            )
            return {"answer": answer}
        validation = state.get("validation")
        if state.get("terminal_status") == "failed" or not validation or not validation.passed:
            reason = state.get("terminal_reason") or (
                "Validation failed: " + ", ".join(validation.failures if validation else [])
            )
            answer = AnswerEnvelope(
                status="failed", intent=state["intent"], policy_version=policy_version,
                attempts=attempts, text=reason,
                limitations=["The agent refused to produce an unvalidated result."],
            )
            return {"answer": answer}
        rows = state["rows"]
        metrics = validation.metrics
        identity = state["identity"]
        text = (
            f"### Orange Book cohort\n\n"
            f"**{identity.ingredient} · {identity.strength} · {identity.dosage_form_route}**\n\n"
            f"Found **{metrics['a_rated_non_reference']} non-reference A-rated products** "
            f"in {attempts} attempt{'s' if attempts != 1 else ''}.\n\n{_table(rows)}\n\n"
            "A-prefix TE codes are federal Orange Book evidence. State substitution law is not evaluated."
        )
        answer = AnswerEnvelope(
            status="answered", intent=state["intent"], policy_version=policy_version,
            attempts=attempts,
            resolved_entities=identity.model_dump(), result_summary=metrics,
            claims=[{"type": "federal_te_evidence", "count": metrics["a_rated_non_reference"]}],
            limitations=["State substitution law is not evaluated."], text=text,
        )
        return {"answer": answer}

    def run(self, question: str, policy: QueryPolicy | None = None,
            persist: bool = True) -> QueryState:
        policy = policy or load_active_policy()
        run_id = str(uuid.uuid4())
        initial: QueryState = {
            "run_id": run_id,
            "question": question,
            "policy": policy,
            "repairs": [],
            "attempt_traces": [],
            "attempt": 0,
            "rows": [],
        }
        state = self.graph.invoke(
            initial,
            {"configurable": {"thread_id": run_id}},
        )
        if persist:
            self.store.save_episode(state)
        return state

    def close(self):
        self.executor.close()
