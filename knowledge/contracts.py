"""Reviewed expectations and application evidence, independent of navigation paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UIAssertion(StrictModel):
    selector: str
    kind: Literal["visible", "count", "text", "value"] = "visible"
    expected: Any = True


class APIAssertion(StrictModel):
    # Same-origin relative GET only. Never generated executable Python.
    path: str
    json_path: list[str | int] = Field(default_factory=list)
    expected: Any = None
    status: int = Field(default=200, ge=100, le=599)
    timeout_ms: int = Field(default=3000, ge=1, le=60000)

    @model_validator(mode="after")
    def relative(self):
        if not self.path.startswith("/") or self.path.startswith("//") or "\\" in self.path:
            raise ValueError("API assertions require an origin-relative path")
        return self


class FixtureAction(StrictModel):
    kind: Literal["click", "fill", "select", "check"]
    selector: str
    value: str | bool | None = None

    @model_validator(mode="after")
    def value_required(self):
        if self.kind in ("fill", "select") and not isinstance(self.value, str):
            raise ValueError("fill/select require a string value")
        if self.kind == "check" and not isinstance(self.value, bool):
            raise ValueError("check requires a boolean value")
        return self


class BranchEvidence(StrictModel):
    branch: str
    explored: bool = False
    evidence: str
    missing: str
    recommendation: str


class BehaviorContract(StrictModel):
    version: str = "1"
    capability: str
    invariant: str
    expected_relation: str
    precondition: str | None = None
    review_status: Literal["inferred", "confirmed"] = "inferred"
    reviewed_by: str | None = None
    requirement_source: str | None = None
    evidence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    fixture_version: str | None = None
    session_scoped: bool = True
    setup: list[FixtureAction] = Field(default_factory=list)
    preconditions: list[UIAssertion] = Field(default_factory=list)
    ready: list[UIAssertion] = Field(default_factory=list)
    api_assertions: list[APIAssertion] = Field(default_factory=list)
    cleanup: list[FixtureAction] = Field(default_factory=list)
    cleanup_assertions: list[UIAssertion] = Field(default_factory=list)
    ignored_observations: dict[str, str] = Field(default_factory=dict)
    branches: list[BranchEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def reviewed(self):
        from knowledge.engine import Relation
        Relation.parse(self.expected_relation)
        if self.precondition:
            Relation.parse(self.precondition)
        if self.review_status == "confirmed" and not (self.reviewed_by and self.requirement_source and self.fixture_version):
            raise ValueError("confirmed contracts require reviewed_by, requirement_source and fixture_version")
        if self.review_status == "confirmed" and not self.preconditions:
            raise ValueError("confirmed contracts require observable fixture preconditions")
        if any(not reason.strip() for reason in self.ignored_observations.values()):
            raise ValueError("ignored observations require a reason")
        if not self.session_scoped and not (self.cleanup and self.cleanup_assertions):
            raise ValueError("non-session-scoped flows require cleanup and verification")
        return self

    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def load_contracts(path):
    if path is None:
        return {}
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("contracts must be an object keyed by test id")
    return {key: BehaviorContract.model_validate(value) for key, value in data.items()}


def apply_contract(instance, overrides):
    from dataclasses import replace
    contract = overrides.get(instance.test_id)
    if contract is None:
        return instance
    if (contract.capability, contract.invariant) != (instance.capability, instance.invariant):
        raise ValueError(f"{instance.test_id}: contract does not match the flow")
    return replace(instance, contract=contract.model_dump(mode="json"),
                   expected_relation=contract.expected_relation, precondition=contract.precondition)


def suggestions(contract):
    """Only use explicitly supplied branch evidence; absence in a crawl is not proof."""
    return [{"branch": b.branch, "evidence": b.evidence,
             "gap": b.missing, "recommendation": b.recommendation,
             "status": "observed_gap" if b.explored else "not_observed",
             "confidence": "branch evidence supplied" if b.explored else "limited: branch untested",
             "scope": "declared branch evidence; not a confirmed defect"}
            for b in contract.branches]


def inferred_contract(candidate, invariant, setup_state):
    models = json.loads(Path(__file__).with_name("capability_models.json").read_text())
    model = models.get(candidate.capability.value, {})
    return BehaviorContract(
        capability=candidate.capability.value, invariant=invariant.id,
        expected_relation=invariant.expected_relation.source,
        precondition=invariant.precondition.source if invariant.precondition else None,
        evidence=[candidate.reasoning_summary,
                  f"setup={setup_state}; goal={candidate.goal_state_id}"],
        assumptions=["Rule applicability is inferred and requires review",
                     "Fixture data is scoped to the browser session"] + model.get("assumptions", []),
        branches=model.get("branches", []),
    ).model_dump(mode="json")
