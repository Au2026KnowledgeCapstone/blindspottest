"""Versioned semantics and execution health, separate from correctness."""
import hashlib
import json
from urllib.parse import urlsplit

EVALUATOR_VERSION = "relations-3"


def semantics(instance):
    from knowledge.contracts import BehaviorContract
    keys = ("capability", "invariant", "expected_relation", "precondition", "setup", "goal",
            "bindings", "procedure", "teardown", "probe_mode", "probe_path")
    data = {key: instance.get(key) for key in keys}
    data["identity_policy"] = instance.get("identity_policy", {})
    data['observation_points'] = instance.get('observation_points',[])
    data['expectation'] = instance.get('expectation',{})
    data["rule_version"] = instance.get("rule_version", "1")
    contract = instance.get("contract")
    data["contract"] = BehaviorContract.model_validate(contract).model_dump(mode="json") if contract else {}
    base = instance.get("base_url", "").rstrip("/")
    entry = instance.get("entry_url", instance.get("url", ""))
    data["entry"] = entry[len(base):] if base and entry.startswith(base + "/") else urlsplit(entry).path
    # Page instances also carry targets, mutations and commit selectors.
    for key in ("target", "mutation", "commit", "steps"):
        if key in instance:
            data[key] = instance[key]
    return data


def signature(instance):
    return hashlib.sha256(json.dumps(semantics(instance), sort_keys=True).encode()).hexdigest()


def comparable(before, after):
    return (signature(before.instance) == signature(after.instance)
            and before.provenance.get("evaluator_version") == after.provenance.get("evaluator_version"))


def execution(result):
    observations = result.observations
    failed = bool(result.error or getattr(result, "reset", None) is False
                  or getattr(result, "restored", None) is False
                  or observations.get("goal_reached") is False
                  or observations.get("setup_reached") is False)
    failed_steps = [step.name for step in result.steps if not step.ok]
    notes = result.notes
    failed = failed or bool(notes.get('observation_errors'))
    return {"status": "failed" if failed else "completed",
            "failed_step": failed_steps[0] if failed_steps else notes.get("goal_failed_at"),
            "detail": result.error or notes.get("goal_failed_at") or notes.get("goal_arrived_at"),
            "cleanup": getattr(result, "reset", getattr(result, "restored", None))}


def comparison_status(before, after):
    if before is None:
        return "new_coverage"
    if not comparable(before, after):
        return "not_comparable"
    if after.execution.get("status") == "failed" or after.error or after.status == "inconclusive":
        return "execution_failure"
    if before.expectation_status == "holds" and after.expectation_status == "violated":
        return "regression_candidate"
    if before.expectation_status == after.expectation_status == "violated":
        return "existing_violation"
    if before.expectation_status != "holds" and after.expectation_status == "holds":
        return "fixed"
    return "unchanged" if before.expectation_status == after.expectation_status else "changed"
