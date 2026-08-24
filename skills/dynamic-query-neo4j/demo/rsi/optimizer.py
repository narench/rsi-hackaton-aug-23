from __future__ import annotations

import json
import uuid
from typing import Protocol

from .models import PolicyOperation, PolicyPatch


class CandidateModel(Protocol):
    def propose(self, parent_version: int, failure_codes: list[str],
                candidate_count: int = 3) -> list[PolicyPatch]: ...


class HeuristicCandidateModel:
    """Deterministic demo mutator; replace through CandidateModel for an LLM proposer."""

    def propose(self, parent_version: int, failure_codes: list[str],
                candidate_count: int = 3) -> list[PolicyPatch]:
        if not 1 <= candidate_count <= 3:
            raise ValueError("candidate_count must be between 1 and 3")
        if "MIXED_STRENGTH" not in failure_codes and "MIXED_FORM_ROUTE" not in failure_codes:
            return []
        patches = [
            PolicyPatch(
                candidate_id=f"pharm-equivalence-{uuid.uuid4().hex[:8]}",
                parent_version=parent_version,
                reason="Use ingredient, strength, dosage form, and route as the pharmaceutical-equivalence grain.",
                operations=[
                    PolicyOperation(op="add_required_identity_field", intent="substitution", field="strength"),
                    PolicyOperation(op="add_required_identity_field", intent="substitution", field="dosage_form_route"),
                    PolicyOperation(op="add_required_identity_field", intent="substitution", field="te_code"),
                    PolicyOperation(op="add_cohort_field", intent="substitution", field="strength"),
                    PolicyOperation(op="add_cohort_field", intent="substitution", field="dosage_form_route"),
                    PolicyOperation(op="add_cohort_field", intent="substitution", field="te_code"),
                    PolicyOperation(
                        op="enable_capability", intent="substitution",
                        field="biologic_interchangeability",
                    ),
                ],
            ),
            PolicyPatch(
                candidate_id=f"strength-only-{uuid.uuid4().hex[:8]}",
                parent_version=parent_version,
                reason="Require and cohort by strength after mixed-strength failures.",
                operations=[
                    PolicyOperation(op="add_required_identity_field", intent="substitution", field="strength"),
                    PolicyOperation(op="add_cohort_field", intent="substitution", field="strength"),
                ],
            ),
            PolicyPatch(
                candidate_id=f"query-only-{uuid.uuid4().hex[:8]}",
                parent_version=parent_version,
                reason="Narrow execution by strength but do not require clarification.",
                operations=[
                    PolicyOperation(op="add_cohort_field", intent="substitution", field="strength"),
                ],
            ),
        ]
        return patches[:candidate_count]


def failure_codes_from_episodes(episodes: list[dict]) -> list[str]:
    codes = []
    for episode in episodes:
        repairs = json.loads(episode["repairs_json"])
        for repair in repairs:
            for code in repair.get("failures", []):
                if code not in codes:
                    codes.append(code)
    return codes
