from __future__ import annotations

import argparse
import json
import sys

from .offline import OfflineRSI
from .online import OnlineQueryAgent
from .policy import activate_version, load_active_policy

DEFAULT_QUESTION = "Find substitutes for Lipitor 10 mg oral tablets"


def print_online(state):
    answer = state["answer"]
    print(f"policy=v{answer.policy_version} status={answer.status} attempts={answer.attempts}")
    validation = state.get("validation")
    if validation:
        print("validation:", json.dumps(validation.model_dump(), indent=2))
    if state.get("repairs"):
        print("online repairs:")
        for repair in state["repairs"]:
            print(json.dumps(repair.model_dump(), indent=2))
    print("\n" + answer.text)


def run_question(question: str, persist: bool = True):
    agent = OnlineQueryAgent()
    try:
        state = agent.run(question, persist=persist)
        print_online(state)
        return state
    finally:
        agent.close()


def improve(approve: bool):
    workflow = OfflineRSI()
    try:
        thread_id, result = workflow.start()
        interrupts = result.get("__interrupt__", [])
        if not interrupts:
            print(json.dumps({k: v for k, v in result.items() if k not in {"episodes"}}, indent=2, default=str))
            return result
        proposal = interrupts[0].value
        print("promotion candidate:")
        print(json.dumps(proposal, indent=2))
        if not approve:
            print(f"paused for approval; thread_id={thread_id}")
            return result
        final = workflow.resume(thread_id, approved=True)
        print(f"promotion status={final.get('status')} path={final.get('promoted_path')}")
        return final
    finally:
        workflow.close()


def resume_decision(thread_id: str, approved: bool):
    workflow = OfflineRSI()
    try:
        final = workflow.resume(thread_id, approved=approved)
        print(f"status={final.get('status')} path={final.get('promoted_path')}")
        return final
    finally:
        workflow.close()


def main():
    parser = argparse.ArgumentParser(description="FDA graph online repair and offline RSI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ask = subparsers.add_parser("ask", help="run the online query loop")
    ask.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    improve_parser = subparsers.add_parser("improve", help="evaluate policy descendants")
    improve_parser.add_argument("--approve", action="store_true")
    approve = subparsers.add_parser("approve", help="resume and approve a paused promotion")
    approve.add_argument("thread_id")
    reject = subparsers.add_parser("reject", help="resume and reject a paused promotion")
    reject.add_argument("thread_id")
    subparsers.add_parser("reset", help="activate baseline policy v1")
    rollback = subparsers.add_parser("rollback", help="activate an existing policy version")
    rollback.add_argument("version", type=int)
    demo = subparsers.add_parser("demo", help="run failure, promotion, and replay")
    demo.add_argument("--question", default=DEFAULT_QUESTION)

    args = parser.parse_args()
    if args.command == "ask":
        run_question(args.question)
    elif args.command == "improve":
        improve(args.approve)
    elif args.command == "approve":
        resume_decision(args.thread_id, approved=True)
    elif args.command == "reject":
        resume_decision(args.thread_id, approved=False)
    elif args.command == "reset":
        path = activate_version(1)
        print(f"active policy reset to {path}")
    elif args.command == "rollback":
        path = activate_version(args.version)
        print(f"active policy rolled back to {path}")
    elif args.command == "demo":
        activate_version(1)
        print("\n=== BEFORE: online self-correction under policy v1 ===")
        run_question(args.question)
        print("\n=== OFFLINE RSI: candidate evaluation and promotion ===")
        improve(approve=True)
        print("\n=== AFTER: replay under promoted policy ===")
        run_question(args.question)


if __name__ == "__main__":
    main()
