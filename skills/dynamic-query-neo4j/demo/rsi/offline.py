from __future__ import annotations

import difflib
import sqlite3
import uuid
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
import yaml

from .evaluator import evaluate_policy
from .models import CandidateEvaluation, PolicyPatch, QueryPolicy
from .online import OnlineQueryAgent
from .optimizer import HeuristicCandidateModel, failure_codes_from_episodes
from .policy import apply_patch, load_active_policy, promote
from .store import DEFAULT_DB, EpisodeStore


class RSIState(TypedDict, total=False):
    thread_id: str
    parent: dict[str, Any]
    episodes: list[dict[str, Any]]
    failure_codes: list[str]
    patches: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    baseline: dict[str, Any]
    evaluations: list[dict[str, Any]]
    winner: dict[str, Any] | None
    decision: dict[str, Any] | None
    status: str
    promoted_path: str | None


class OfflineRSI:
    def __init__(self, online_agent: OnlineQueryAgent | None = None,
                 store: EpisodeStore | None = None,
                 candidate_count: int = 3):
        if not 1 <= candidate_count <= 3:
            raise ValueError("candidate_count must be between 1 and 3")
        self.store = store or EpisodeStore()
        self.candidate_count = candidate_count
        self.online = online_agent or OnlineQueryAgent(store=self.store)
        self.candidate_model = HeuristicCandidateModel()
        checkpoint_path = Path(DEFAULT_DB).with_name("langgraph-checkpoints.db")
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self.graph = self._build_graph().compile(
            checkpointer=SqliteSaver(self.checkpoint_connection)
        )

    def _build_graph(self):
        graph = StateGraph(RSIState)
        graph.add_node("collect", self.collect)
        graph.add_node("propose", self.propose)
        graph.add_node("evaluate", self.evaluate)
        graph.add_node("select", self.select)
        graph.add_node("approval", self.approval)
        graph.add_node("promote", self.promote_winner)
        graph.add_edge(START, "collect")
        graph.add_conditional_edges("collect", self.route_collect, {
            "propose": "propose", "end": END,
        })
        graph.add_conditional_edges("propose", self.route_propose, {
            "evaluate": "evaluate", "end": END,
        })
        graph.add_edge("evaluate", "select")
        graph.add_conditional_edges("select", self.route_select, {
            "approval": "approval", "end": END,
        })
        graph.add_conditional_edges("approval", self.route_approval, {
            "promote": "promote", "end": END,
        })
        graph.add_edge("promote", END)
        return graph

    def collect(self, state: RSIState) -> dict:
        episodes = self.store.recent_repairable_failures()
        if not episodes:
            return {"episodes": [], "status": "no_repairable_episodes"}
        return {
            "episodes": episodes,
            "failure_codes": failure_codes_from_episodes(episodes),
            "status": "collected",
        }

    @staticmethod
    def route_collect(state: RSIState) -> str:
        return "propose" if state.get("episodes") else "end"

    def propose(self, state: RSIState) -> dict:
        parent = QueryPolicy.model_validate(state["parent"])
        patches = self.candidate_model.propose(
            parent.version, state["failure_codes"], self.candidate_count
        )
        if not patches:
            return {"patches": [], "status": "no_candidates"}
        candidates = [
            apply_patch(parent, patch, version=parent.version + 1).model_dump()
            for patch in patches
        ]
        return {
            "patches": [patch.model_dump() for patch in patches],
            "candidates": candidates,
            "status": "proposed",
        }

    @staticmethod
    def route_propose(state: RSIState) -> str:
        return "evaluate" if state.get("patches") else "end"

    def evaluate(self, state: RSIState) -> dict:
        parent = QueryPolicy.model_validate(state["parent"])
        baseline = evaluate_policy(self.online, parent, candidate_id="baseline")
        evaluations = []
        for patch_data, policy_data in zip(state["patches"], state["candidates"]):
            patch = PolicyPatch.model_validate(patch_data)
            policy = QueryPolicy.model_validate(policy_data)
            result = evaluate_policy(self.online, policy, candidate_id=patch.candidate_id)
            parent_passed = {case.case_id for case in baseline.cases if case.passed}
            candidate_failed = {case.case_id for case in result.cases if not case.passed}
            result.regressions = len(parent_passed & candidate_failed)
            self.store.save_evaluation(
                parent.version, patch.candidate_id, policy.model_dump(), result.model_dump()
            )
            evaluations.append(result.model_dump())
        return {
            "baseline": baseline.model_dump(),
            "evaluations": evaluations,
            "status": "evaluated",
        }

    def select(self, state: RSIState) -> dict:
        baseline = CandidateEvaluation.model_validate(state["baseline"])
        eligible = [
            CandidateEvaluation.model_validate(item)
            for item in state["evaluations"]
            if item["passed"] and item["regressions"] == 0
            and not item["hard_failures"] and item["score"] > baseline.score
        ]
        if not eligible:
            return {"winner": None, "status": "no_eligible_candidate"}
        best = max(eligible, key=lambda item: item.score)
        index = next(
            i for i, patch in enumerate(state["patches"])
            if patch["candidate_id"] == best.candidate_id
        )
        return {
            "winner": {
                "patch": state["patches"][index],
                "policy": state["candidates"][index],
                "evaluation": best.model_dump(),
            },
            "status": "winner_selected",
        }

    @staticmethod
    def route_select(state: RSIState) -> str:
        return "approval" if state.get("winner") else "end"

    @staticmethod
    def approval(state: RSIState) -> dict:
        winner = state["winner"]
        parent_yaml = yaml.safe_dump(state["parent"], sort_keys=False).splitlines()
        candidate_yaml = yaml.safe_dump(winner["policy"], sort_keys=False).splitlines()
        policy_diff = "\n".join(difflib.unified_diff(
            parent_yaml,
            candidate_yaml,
            fromfile=f"query-policy.v{state['parent']['version']}.yaml",
            tofile=f"query-policy.v{winner['policy']['version']}.yaml",
            lineterm="",
        ))
        decision = interrupt({
            "kind": "policy_promotion",
            "candidate_id": winner["patch"]["candidate_id"],
            "reason": winner["patch"]["reason"],
            "baseline_score": state["baseline"]["score"],
            "candidate_score": winner["evaluation"]["score"],
            "regressions": winner["evaluation"]["regressions"],
            "operations": winner["patch"]["operations"],
            "policy_diff": policy_diff,
        })
        return {"decision": decision}

    @staticmethod
    def route_approval(state: RSIState) -> str:
        decision = state.get("decision") or {}
        return "promote" if decision.get("approved") else "end"

    def promote_winner(self, state: RSIState) -> dict:
        winner = state["winner"]
        policy = QueryPolicy.model_validate(winner["policy"])
        evaluation = CandidateEvaluation.model_validate(winner["evaluation"])
        path = promote(policy, evaluation.score)
        self.store.save_promotion(
            policy.version, policy.parent, winner["patch"]["candidate_id"], evaluation.score
        )
        return {"status": "promoted", "promoted_path": str(path)}

    def start(self, thread_id: str | None = None) -> tuple[str, dict]:
        thread_id = thread_id or str(uuid.uuid4())
        parent = load_active_policy()
        state: RSIState = {"thread_id": thread_id, "parent": parent.model_dump(), "status": "started"}
        result = self.graph.invoke(state, {"configurable": {"thread_id": thread_id}})
        return thread_id, result

    def resume(self, thread_id: str, approved: bool) -> dict:
        return self.graph.invoke(
            Command(resume={"approved": approved}),
            {"configurable": {"thread_id": thread_id}},
        )

    def close(self):
        self.online.close()
        self.checkpoint_connection.close()
