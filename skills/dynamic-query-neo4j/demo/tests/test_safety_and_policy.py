from __future__ import annotations

import pytest

from rsi.executor import GraphExecutor
from rsi.models import PolicyOperation, PolicyPatch
from rsi.policy import apply_patch, load_policy


@pytest.mark.parametrize("cypher", [
    "CREATE (n)",
    "MATCH (n) DELETE n",
    "CALL db.labels()",
    "MATCH (n) RETURN n; MATCH (m) RETURN m",
    "MATCH (n) /* hidden */ DELETE n",
    "SHOW USERS",
])
def test_read_only_boundary_rejects_unsafe_queries(cypher):
    executor = GraphExecutor.__new__(GraphExecutor)
    with pytest.raises(ValueError):
        executor.run(cypher)


def test_patch_parent_must_match():
    parent = load_policy("rsi/policies/query-policy.v1.yaml")
    patch = PolicyPatch(
        candidate_id="bad-parent", parent_version=99, reason="bad",
        operations=[PolicyOperation(
            op="add_cohort_field", intent="substitution", field="strength"
        )],
    )
    with pytest.raises(ValueError, match="does not match"):
        apply_patch(parent, patch)


def test_unknown_policy_field_is_rejected():
    parent = load_policy("rsi/policies/query-policy.v1.yaml")
    patch = PolicyPatch(
        candidate_id="bad-field", parent_version=1, reason="bad",
        operations=[PolicyOperation(
            op="add_cohort_field", intent="substitution", field="patient_name"
        )],
    )
    with pytest.raises(ValueError, match="unsupported cohort field"):
        apply_patch(parent, patch)
