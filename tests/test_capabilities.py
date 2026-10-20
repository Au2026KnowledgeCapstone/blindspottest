"""The capability definition layer, and the two bugs that motivated it.

No browser, no LLM, no credentials.
"""
import unittest

from knowledge.capabilities import (
    CapabilityError,
    load,
    render_definitions,
    unsupported,
)
from knowledge.flows import FlowEngine
from semantic.backends import strict_schema
from semantic.flow_classifier import build_system_prompt
from semantic.flow_schemas import FlowCapability, flow_candidate_set_schema
from semantic.page_schemas import candidate_set_schema


def ref_siblings(schema) -> list[tuple[str, list[str]]]:
    """Every `$ref` that carries other keywords, with their names."""
    found = []

    def walk(node, path=()):
        if isinstance(node, dict):
            if "$ref" in node and len(node) > 1:
                found.append((".".join(map(str, path)),
                              sorted(k for k in node if k != "$ref")))
            for key, value in node.items():
                walk(value, path + (key,))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + (i,))

    walk(schema)
    return found


class Definitions(unittest.TestCase):
    def test_every_capability_is_defined(self):
        """A capability the model can emit but nothing defines is the hole
        this layer exists to close."""
        defined = load()
        for capability in FlowCapability:
            self.assertIn(capability.value, defined)

    def test_no_definition_without_an_enum_member(self):
        defined = set(load())
        self.assertEqual(defined - {c.value for c in FlowCapability}, set())

    def test_supported_flag_matches_the_rule_base(self):
        """`supported` is a claim about the rule base, so it has to track it:
        a capability marked supported with no invariant silently drops
        candidates, and the reverse hides a test that would have run."""
        engine = FlowEngine.load()
        for name, definition in load().items():
            self.assertEqual(
                definition.supported,
                bool(engine.invariants_for(name)),
                f"{name}: supported={definition.supported} but "
                f"invariants={[i.id for i in engine.invariants_for(name)]}",
            )

    def test_unsupported_capabilities_explain_themselves(self):
        gaps = unsupported()
        self.assertTrue(gaps)
        for name, reason in gaps.items():
            self.assertTrue(reason.strip(), f"{name} has an empty gap reason")

    def test_definitions_carry_exclusions(self):
        """The NOT lines are the working half: an archive matches every
        positive signal a deletion has."""
        deletion = load()["delete_resource"]
        self.assertTrue(deletion.signals)
        joined = " ".join(deletion.excludes).lower()
        self.assertIn("archiv", joined)

    def test_render_mentions_every_capability(self):
        rendered = render_definitions()
        for capability in FlowCapability:
            self.assertIn(capability.value, rendered)

    def test_missing_file_is_reported(self):
        with self.assertRaises(CapabilityError):
            load("knowledge/does-not-exist.json")


class SystemPrompt(unittest.TestCase):
    def test_prompt_carries_the_definitions(self):
        prompt = build_system_prompt()
        self.assertIn("CAPABILITIES", prompt)           # the static preamble
        self.assertIn("WHAT EACH CAPABILITY MEANS", prompt)
        for capability in FlowCapability:
            self.assertIn(capability.value, prompt)

    def test_unsupported_capabilities_are_marked_in_the_prompt(self):
        """They stay proposable — a named gap beats silence — but the model
        is told they will not be tested."""
        prompt = build_system_prompt()
        self.assertIn("no invariant exists for this yet", prompt)


class NoInvariantIsReported(unittest.TestCase):
    """The silent drop: a candidate whose capability has no invariant used to
    produce neither an instance nor a skip, while the record claimed
    `skipped: 0`."""

    class Stub:
        def __init__(self, capability):
            self.capability = capability

    def test_capability_without_an_invariant_is_skipped(self):
        engine = FlowEngine.load()
        missing = [c for c in FlowCapability if not engine.invariants_for(c.value)]
        self.assertTrue(missing, "expected at least one uncovered capability")

        for capability in missing:
            instances, skipped = engine.instantiate(
                [self.Stub(capability)], graph=None
            )
            self.assertEqual(instances, [])
            self.assertEqual(len(skipped), 1)
            self.assertTrue(skipped[0].unmet[0].strip())

    def test_skip_reason_comes_from_the_definitions(self):
        """The capability is taken from the rule base rather than named, so
        this keeps testing the no-invariant path as invariants are added.
        Naming one meant the test broke the day `authenticate` gained an
        invariant — and it broke by crashing on `graph=None`, which only the
        no-invariant path is allowed to be passed."""
        engine = FlowEngine.load()
        gaps = unsupported()
        self.assertTrue(gaps, "no uncovered capability left to test with")
        name = sorted(gaps)[0]

        _, skipped = engine.instantiate(
            [self.Stub(FlowCapability(name))], graph=None
        )
        self.assertEqual(skipped[0].unmet[0], gaps[name])


class StrictSchemas(unittest.TestCase):
    """OpenAI rejects the whole request with "$ref cannot have keywords
    {'description'}" — which is what a Field(description=...) on an enum or
    model-typed field produces."""

    def test_no_ref_carries_siblings(self):
        for name, schema in (
            ("flow candidates", flow_candidate_set_schema()),
            ("page candidates", candidate_set_schema()),
        ):
            self.assertEqual(ref_siblings(schema), [], name)

    def test_the_ref_target_keeps_its_documentation(self):
        """Only the duplicated sibling is dropped; the $defs entry the ref
        points at still describes itself."""
        schema = flow_candidate_set_schema()
        extract = schema["$defs"]["ObservationBinding"]["properties"]["extract"]
        self.assertEqual(list(extract), ["$ref"])
        self.assertIn("description", schema["$defs"]["ExtractKind"])

    def test_validation_keywords_are_still_stripped(self):
        from pydantic import BaseModel, Field

        class Bounded(BaseModel):
            score: float = Field(ge=0.0, le=1.0)

        self.assertNotIn("minimum", str(strict_schema(Bounded)))


if __name__ == "__main__":
    unittest.main()
