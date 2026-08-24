from __future__ import annotations

from rsi.models import PolicyOperation, PolicyPatch, ResolvedIdentity
from rsi.online import OnlineQueryAgent
from rsi.policy import apply_patch, load_policy


class FakeExecutor:
    def resolve_name(self, question):
        return "atorvastatin calcium" if "atorvastatin" in question.lower() else "lipitor"

    def resolve_orange_identity(self, question, term):
        has_strength = "10 mg" in question.lower() or "20 mg" in question.lower()
        strength = "EQ 20MG BASE" if "20 mg" in question.lower() else "EQ 10MG BASE"
        alternatives = [
            {"id": "NDA020702:001", "ingredient": "ATORVASTATIN CALCIUM",
             "strength": "EQ 10MG BASE", "dosage_form_route": "TABLET;ORAL", "rld": "Yes"},
            {"id": "NDA020702:002", "ingredient": "ATORVASTATIN CALCIUM",
             "strength": "EQ 20MG BASE", "dosage_form_route": "TABLET;ORAL", "rld": "Yes"},
        ]
        return ResolvedIdentity(
            term=term, ingredient="ATORVASTATIN CALCIUM",
            strength=strength if has_strength else None,
            dosage_form_route="TABLET;ORAL" if has_strength else None,
            te_code="AB" if has_strength else None,
            selected_product_id="NDA020702:001", alternatives=alternatives,
        )

    def run(self, cypher, **params):
        strengths = [params["strength"]] if "strength" in params else ["EQ 10MG BASE", "EQ 20MG BASE"]
        rows = []
        for strength in strengths:
            rows.extend([
                {"application": "NDA020702", "product_id": "ref", "product": "LIPITOR",
                 "ingredient": "ATORVASTATIN CALCIUM", "strength": strength,
                 "dosage_form_route": "TABLET;ORAL", "manufacturer": "PFIZER",
                 "approval": "Dec 17, 1996", "rld": "Yes", "rs": "Yes", "te_code": "AB"},
                {"application": "ANDA000001", "product_id": "generic", "product": "ATORVASTATIN",
                 "ingredient": "ATORVASTATIN CALCIUM", "strength": strength,
                 "dosage_form_route": "TABLET;ORAL", "manufacturer": "GENERIC CO",
                 "approval": "Jan 1, 2012", "rld": "No", "rs": "No", "te_code": "AB"},
            ])
        return rows

    def close(self):
        pass


class NullStore:
    def save_episode(self, state):
        pass


def baseline():
    return load_policy("rsi/policies/query-policy.v1.yaml")


def improved(parent):
    patch = PolicyPatch(
        candidate_id="test", parent_version=parent.version, reason="exact cohort",
        operations=[
            PolicyOperation(op="add_required_identity_field", intent="substitution", field="strength"),
            PolicyOperation(op="add_required_identity_field", intent="substitution", field="dosage_form_route"),
            PolicyOperation(op="add_required_identity_field", intent="substitution", field="te_code"),
            PolicyOperation(op="add_cohort_field", intent="substitution", field="strength"),
            PolicyOperation(op="add_cohort_field", intent="substitution", field="dosage_form_route"),
            PolicyOperation(op="add_cohort_field", intent="substitution", field="te_code"),
        ],
    )
    return apply_patch(parent, patch)


def test_baseline_repairs_mixed_strength_online():
    agent = OnlineQueryAgent(executor=FakeExecutor(), store=NullStore())
    state = agent.run("Find substitutes for Lipitor 10 mg oral tablets", policy=baseline(), persist=False)
    assert state["answer"].status == "answered"
    assert state["answer"].attempts == 2
    assert state["repairs"][0].failures == ["MIXED_STRENGTH"]


def test_improved_policy_passes_first_attempt():
    agent = OnlineQueryAgent(executor=FakeExecutor(), store=NullStore())
    state = agent.run(
        "Find substitutes for Lipitor 10 mg oral tablets",
        policy=improved(baseline()), persist=False,
    )
    assert state["answer"].status == "answered"
    assert state["answer"].attempts == 1
    assert state["repairs"] == []


def test_improved_policy_clarifies_before_query():
    agent = OnlineQueryAgent(executor=FakeExecutor(), store=NullStore())
    state = agent.run("Can generic Lipitor be substituted?", policy=improved(baseline()), persist=False)
    assert state["answer"].status == "clarification"
    assert state["answer"].attempts == 0
    assert "strength" in state["answer"].text
