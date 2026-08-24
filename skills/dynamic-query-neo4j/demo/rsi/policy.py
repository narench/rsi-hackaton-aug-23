from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

import yaml

from .models import PolicyPatch, QueryPolicy

ROOT = Path(__file__).resolve().parent
ACTIVE_PATH = ROOT / "active-policy.json"
POLICY_DIR = ROOT / "policies"
ALLOWED_IDENTITY_FIELDS = {"ingredient", "strength", "dosage_form_route", "te_code"}
ALLOWED_CAPABILITIES = {"biologic_interchangeability"}
ALLOWED_REPAIR_ACTIONS = {
    "ask_clarification", "add_pharmaceutical_equivalence_fields",
    "relax_filters_incrementally", "mark_unknown",
}


def load_policy(path: str | Path) -> QueryPolicy:
    with Path(path).open() as handle:
        return QueryPolicy.model_validate(yaml.safe_load(handle))


def load_active_policy() -> QueryPolicy:
    metadata = json.loads(ACTIVE_PATH.read_text())
    return load_policy(ROOT / metadata["path"])


def apply_patch(parent: QueryPolicy, patch: PolicyPatch, version: int | None = None) -> QueryPolicy:
    if patch.parent_version != parent.version:
        raise ValueError(
            f"patch parent v{patch.parent_version} does not match active parent v{parent.version}"
        )
    data = copy.deepcopy(parent.model_dump())
    data["version"] = version if version is not None else parent.version + 1
    data["parent"] = parent.version
    data["description"] = patch.reason
    for operation in patch.operations:
        if operation.op == "enable_capability":
            if not operation.field or operation.field not in ALLOWED_CAPABILITIES:
                raise ValueError(f"unsupported capability: {operation.field}")
            data.setdefault("capabilities", {})[operation.field] = True
            continue
        intent = data["intents"].get(operation.intent)
        if intent is None:
            raise ValueError(f"unknown intent: {operation.intent}")
        if operation.op == "add_required_identity_field":
            if not operation.field or operation.field not in ALLOWED_IDENTITY_FIELDS:
                raise ValueError(f"unsupported identity field: {operation.field}")
            if operation.field not in intent["required_identity"]:
                intent["required_identity"].append(operation.field)
        elif operation.op == "add_cohort_field":
            if not operation.field or operation.field not in ALLOWED_IDENTITY_FIELDS:
                raise ValueError(f"unsupported cohort field: {operation.field}")
            if operation.field not in intent["cohort_fields"]:
                intent["cohort_fields"].append(operation.field)
        elif operation.op == "set_repair":
            if not operation.failure or operation.action not in ALLOWED_REPAIR_ACTIONS:
                raise ValueError(f"unsupported repair action: {operation.action}")
            intent["repairs"][operation.failure] = operation.action
    return QueryPolicy.model_validate(data)


def write_policy(policy: QueryPolicy) -> Path:
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    path = POLICY_DIR / f"query-policy.v{policy.version}.yaml"
    payload = yaml.safe_dump(policy.model_dump(), sort_keys=False)
    if path.exists() and path.read_text() != payload:
        raise FileExistsError(f"policy version already exists with different content: {path}")
    path.write_text(payload)
    return path


def _write_active(metadata: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix="active-policy-", suffix=".json", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(metadata, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, ACTIVE_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def activate_version(version: int, score: float | None = None) -> Path:
    path = POLICY_DIR / f"query-policy.v{version}.yaml"
    policy = load_policy(path)
    _write_active({
        "version": policy.version,
        "path": str(path.relative_to(ROOT)),
        "parent": policy.parent,
        "evaluation_score": score,
    })
    return path


def promote(policy: QueryPolicy, score: float) -> Path:
    path = write_policy(policy)
    metadata = {
        "version": policy.version,
        "path": str(path.relative_to(ROOT)),
        "parent": policy.parent,
        "evaluation_score": round(score, 4),
    }
    _write_active(metadata)
    return path
