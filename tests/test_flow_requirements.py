"""Requirements that assert the walk established what the relation presumes.

flow-007 was a real run against the sound build: its walk was a search
filter, the catalog rendered correctly name-ascending, the candidate bound
the price column, and `sorted_asc(row_values)` reported a violation against
an application that was right. The capability was never exercised — nothing
in the walk sorted anything — and no gate noticed.

`sort_established` and `credentials_submitted` both close that shape: the
first for an ordering relation over an unsorted collection, the second for
an access probe in a session the goal walk did not establish.

No browser, no LLM, no credentials.
"""
import unittest

from discovery.graph import Action, AppGraph, State
from knowledge.flows import FLOW_REQUIREMENT_PREDICATES, FlowEngine
from semantic.flow_schemas import FlowCapability


CATALOG = "s_catalog"
FILTERED = "s_filtered"

# The affordance key of the demo app's sort control, as the crawler records
# it: `role|type|label`, with no type on a <select>.
SORT_CONTROL = "combobox||sort by"
SEARCH_FIELD = "textbox|text|search products"


def graph(*actions: Action, goal_template: str = "/catalog?q,sort") -> AppGraph:
    g = AppGraph(entry=CATALOG, base_url="http://127.0.0.1:3100")
    g.states[CATALOG] = State(
        id=CATALOG, url_template="/catalog", title="Catalog", signature=()
    )
    g.states[FILTERED] = State(
        id=FILTERED, url_template=goal_template, title="Catalog", signature=()
    )
    for action in actions:
        g.connect(action)
    return g


def apply_filters(*inputs: tuple[str, str]) -> Action:
    """The edge flow-007 actually walked: fill a field, submit the form."""
    return Action(
        source=CATALOG,
        target=FILTERED,
        affordance_key="button|submit|apply filters",
        label="Apply filters",
        inputs=tuple(inputs),
    )


class Candidate:
    """Only the attributes a flow requirement predicate reads."""

    def __init__(self, *, setup=CATALOG, goal=FILTERED, probe=None, bindings=(),
                 capability=FlowCapability.SORT_COLLECTION):
        self.capability = capability
        self.setup_state_id = setup
        self.goal_state_id = goal
        self.probe = probe
        self.bindings = bindings


def sort_established(candidate, g) -> bool:
    return FLOW_REQUIREMENT_PREDICATES["sort_established"](candidate, g)


class SortEstablished(unittest.TestCase):
    def test_a_filter_walk_does_not_establish_a_sort(self):
        """The flow-007 shape. Setting the search field and submitting lands
        on a correctly ordered page without having chosen an order."""
        g = graph(apply_filters((SEARCH_FIELD, "a")))
        self.assertFalse(sort_established(Candidate(), g))

    def test_setting_the_sort_control_establishes_a_sort(self):
        g = graph(apply_filters((SEARCH_FIELD, "a"), (SORT_CONTROL, "price-asc")))
        self.assertTrue(sort_established(Candidate(), g))

    def test_a_sort_affordance_fired_without_inputs_counts(self):
        """A link or header that sorts by being clicked carries no input, so
        the affordance's own key has to be enough."""
        g = graph(
            Action(
                source=CATALOG,
                target=FILTERED,
                affordance_key="link||sort by price",
                label="Sort by price",
            )
        )
        self.assertTrue(sort_established(Candidate(), g))

    def test_the_route_mentioning_sort_is_not_enough(self):
        """The design decision, guarded. A state's `url_template` records
        parameter names and not values, so `/catalog?q,sort` is the template
        of the default-ordered page too — flow-007's goal state carried it.
        A predicate written against the template would admit the candidate
        this one exists to reject."""
        g = graph(apply_filters((SEARCH_FIELD, "a")),
                  goal_template="/catalog?q,sort")
        self.assertIn("sort", g.states[FILTERED].url_template)
        self.assertFalse(sort_established(Candidate(), g))

    def test_match_is_word_anchored(self):
        """A substring test would read 'assortment' as a sort control."""
        g = graph(apply_filters(("combobox||assortment", "mixed")))
        self.assertFalse(sort_established(Candidate(), g))

    def test_an_unreachable_goal_is_unmet_not_an_error(self):
        g = graph()  # no edges at all
        self.assertFalse(sort_established(Candidate(), g))

    def test_an_empty_walk_is_unmet(self):
        """Setup == goal is `goal_differs_from_setup`'s job, but this
        predicate is asked first in some orders and must not claim a sort
        happened in zero actions."""
        g = graph(apply_filters((SORT_CONTROL, "price-asc")))
        self.assertFalse(sort_established(Candidate(setup=CATALOG,
                                                    goal=CATALOG), g))


class RuleBase(unittest.TestCase):
    def test_ordering_consistency_requires_it(self):
        """Without this the invariant asserts over whatever order the
        application defaulted to."""
        engine = FlowEngine.load()
        ordering = [
            inv
            for inv in engine.invariants_for(FlowCapability.SORT_COLLECTION.value)
            if inv.id == "ORDERING_CONSISTENCY"
        ]
        self.assertTrue(ordering, "ORDERING_CONSISTENCY is not in the rule base")
        self.assertIn("sort_established", ordering[0].requirements)

    def test_the_filter_walk_is_skipped_with_the_reason_named(self):
        """End to end through the real rule base: the candidate that produced
        the false positive now produces a skip that says why."""
        engine = FlowEngine.load()
        g = graph(apply_filters((SEARCH_FIELD, "a")))
        candidate = Candidate(bindings=())

        instances, skipped = engine.instantiate([candidate], g)

        self.assertEqual(instances, [])
        ordering = [s for s in skipped if s.invariant_id == "ORDERING_CONSISTENCY"]
        self.assertEqual(len(ordering), 1)
        self.assertIn("sort_established", ordering[0].unmet)

    def test_a_sorted_walk_meets_every_requirement(self):
        """The gate has to let the real thing through — checked at the
        requirement level, since building an instance needs a snapshot."""
        engine = FlowEngine.load()
        g = graph(apply_filters((SEARCH_FIELD, "a"), (SORT_CONTROL, "price-asc")))
        candidate = Candidate()

        for inv in engine.invariants_for(FlowCapability.SORT_COLLECTION.value):
            self.assertEqual(inv.unmet_requirements(candidate, g), [], inv.id)


PASSWORD_FIELD = "textbox|password|password"
USERNAME_FIELD = "textbox|text|username"


def credentials_submitted(candidate, g) -> bool:
    return FLOW_REQUIREMENT_PREDICATES["credentials_submitted"](candidate, g)


def sign_in(*inputs: tuple[str, str]) -> Action:
    return Action(
        source=CATALOG,
        target=FILTERED,
        affordance_key="button|submit|sign in",
        label="Sign in",
        inputs=tuple(inputs),
    )


class CredentialsSubmitted(unittest.TestCase):
    def test_a_credential_submission_establishes_a_session(self):
        g = graph(sign_in((USERNAME_FIELD, "demo"), (PASSWORD_FIELD, "demo123")))
        self.assertTrue(credentials_submitted(Candidate(), g))

    def test_reaching_an_authenticated_page_is_not_enough(self):
        """The failure this exists to prevent: the setup walk may have signed
        in on its way to the setup state, so the probe answers 'reachable'
        however little the goal walk did. A navigation with no credentials in
        it is not an authentication."""
        g = graph(
            Action(
                source=CATALOG,
                target=FILTERED,
                affordance_key="link||account",
                label="Account",
            )
        )
        self.assertFalse(credentials_submitted(Candidate(), g))

    def test_a_username_alone_is_not_a_credential_submission(self):
        g = graph(sign_in((USERNAME_FIELD, "demo")))
        self.assertFalse(credentials_submitted(Candidate(), g))

    def test_the_type_segment_is_what_counts(self):
        """A password input declares itself in the key's type segment
        whatever the field is labelled, which is why the key is matched
        whole rather than the label alone."""
        g = graph(sign_in(("textbox|password|secret phrase", "demo123")))
        self.assertTrue(credentials_submitted(Candidate(), g))

    def test_an_unreachable_goal_is_unmet(self):
        self.assertFalse(credentials_submitted(Candidate(), graph()))


class AuthenticationGrantsAccess(unittest.TestCase):
    class Probe:
        path = "/account"

    def invariant(self):
        engine = FlowEngine.load()
        found = [
            inv
            for inv in engine.invariants_for(FlowCapability.AUTHENTICATE.value)
            if inv.id == "AUTHENTICATION_GRANTS_ACCESS"
        ]
        self.assertTrue(found, "AUTHENTICATION_GRANTS_ACCESS is not loaded")
        return found[0]

    def test_authenticate_is_no_longer_a_coverage_gap(self):
        engine = FlowEngine.load()
        self.assertTrue(engine.invariants_for(FlowCapability.AUTHENTICATE.value))

    def test_it_requires_both_a_probe_and_a_credential_submission(self):
        """A probe alone proves the page is reachable, not that this flow is
        why — and credentials alone cannot settle a claim about a session."""
        requirements = self.invariant().requirements
        self.assertIn("probe_declared", requirements)
        self.assertIn("credentials_submitted", requirements)

    def test_it_asserts_the_converse_of_session_termination(self):
        inv = self.invariant()
        self.assertEqual(inv.expected_relation.source, "probe_reachable == true")

    def test_a_sign_in_walk_with_a_probe_meets_every_requirement(self):
        g = graph(sign_in((USERNAME_FIELD, "demo"), (PASSWORD_FIELD, "demo123")))
        candidate = Candidate(capability=FlowCapability.AUTHENTICATE,
                              probe=self.Probe())
        self.assertEqual(self.invariant().unmet_requirements(candidate, g), [])

    def test_a_navigation_walk_is_skipped_with_the_reason_named(self):
        engine = FlowEngine.load()
        g = graph(
            Action(source=CATALOG, target=FILTERED,
                   affordance_key="link||account", label="Account")
        )
        candidate = Candidate(capability=FlowCapability.AUTHENTICATE,
                              probe=self.Probe())

        instances, skipped = engine.instantiate([candidate], g)

        self.assertEqual(instances, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("credentials_submitted", skipped[0].unmet)


if __name__ == "__main__":
    unittest.main()
