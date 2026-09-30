"""Collapse a page snapshot into a stable state identity.

This is the load-bearing piece of flow discovery. Every layer above it —
the graph, the planner, regression diffing — is only as trustworthy as the
answer to one question: *are these two snapshots the same state?*

Get it too strict and `/projects/3` and `/projects/4` become different states,
so a ten-state app crawls as thousands and the graph is useless. Get it too
loose and two pages that behave differently collapse into one node, so the
planner emits paths that cannot be walked.

The rule used here: a state is identified by **what you can do**, not by what
it currently holds. Two snapshots are the same state when they offer the same
affordances at the same URL shape, regardless of the data displayed.

So the fingerprint deliberately ignores:

    values          a filled form and an empty one are the same state
    counts          47 rows and 50 rows are the same collection page
    instance text   "Project 37" and "Project 4" are the same detail page
    ids in the URL  /projects/3 and /projects/4 likewise

and deliberately keeps:

    url template    /projects/<id> is not /projects
    roles + types   a page with a textbox is not a page without one
    control names   "Save" is not "Delete"
    disabled        a disabled Continue is a different affordance set

Whole-graph reproducibility is per-state stability raised to the Nth power:
at 100 states, 99% per-state stability yields a clean crawl only 37% of the
time. That is why this module is small, pure, and tested on its own — it takes
a snapshot dict and returns a string, with no browser anywhere near it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# --------------------------------------------------------------------------
# URL templating
# --------------------------------------------------------------------------

# Path segments that are obviously instance identifiers rather than route
# names. Numeric ids and uuids cover the demo app and most real ones; a
# slug-shaped segment is left alone, because /catalog/widgets and
# /catalog/gadgets may well be genuinely different pages.
_NUMERIC_SEGMENT = re.compile(r"^\d+$")
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_HEX_SEGMENT = re.compile(r"^[0-9a-f]{16,}$", re.I)

# Query keys that carry a one-shot banner rather than page state. `/cart` and
# `/cart?notice=item-removed` are the same place; the flash is gone on the
# next request. Left in, they mint a near-duplicate of every state that can
# be arrived at from a redirect, which is most of them.
TRANSIENT_QUERY_KEYS = frozenset({
    "notice", "saved", "flash", "message", "msg", "error", "alert",
    "status", "toast", "_", "t", "ts", "cachebust",
})


def url_template(url: str, *, keep_query_keys: bool = True) -> str:
    """Normalize a URL to the route it represents.

    Query *keys* are kept and query *values* dropped, which is the right call
    for collection pages: `?sort=price-asc` and `?sort=name-desc` are the same
    state — a filtered catalog — reached with different arguments. Keeping the
    values would mint a new state per filter combination and explode the graph
    on exactly the pages most worth testing.
    """
    parts = urlsplit(url)

    segments = []
    for segment in parts.path.split("/"):
        if not segment:
            continue
        if (
            _NUMERIC_SEGMENT.match(segment)
            or _UUID_SEGMENT.match(segment)
            or _HEX_SEGMENT.match(segment)
        ):
            segments.append("<id>")
        else:
            segments.append(segment.lower())

    path = "/" + "/".join(segments)

    if keep_query_keys and parts.query:
        # Sorted so parameter order never changes the identity, and empty
        # parameters dropped so `?q=&sort=` matches a bare `?sort=`.
        keys = sorted(
            k for k in parse_qs(parts.query, keep_blank_values=False)
            if k.lower() not in TRANSIENT_QUERY_KEYS
        )
        if keys:
            path += "?" + ",".join(keys)

    return path


# --------------------------------------------------------------------------
# Text normalization
# --------------------------------------------------------------------------

_DIGIT_RUN = re.compile(r"\d[\d,.\s]*")
_NON_WORD = re.compile(r"[^a-z0-9#]+")


def normalize_text(text: str | None) -> str:
    """Strip instance data out of a control's name.

    Digit runs become `#`, so "Project 37", "$1,299.00" and "3 items" reduce
    to "project #", "$#" and "# items". That is what makes one detail page
    stand for all of them.
    """
    if not text:
        return ""
    lowered = text.lower()
    lowered = _DIGIT_RUN.sub("#", lowered)
    lowered = _NON_WORD.sub(" ", lowered)
    return " ".join(lowered.split())


# --------------------------------------------------------------------------
# Element signatures
# --------------------------------------------------------------------------

# Keys that describe what an element *is*. Everything else in a snapshot
# entry describes what it currently holds, and is excluded on purpose.
def element_signature(element: dict) -> str:
    """A compact description of one control's identity, free of its value."""
    role = element.get("role") or element.get("tag", "")
    type_ = element.get("type", "")

    # The name a user would use to refer to this control. `text` first for
    # buttons and links, since that is their identity; `label` first for
    # fields. `accessible_name` already encodes that preference.
    name = (
        element.get("accessible_name")
        or element.get("text")
        or element.get("label")
        or element.get("placeholder")
        or ""
    )

    parts = [role, type_, normalize_text(name)]

    # Availability changes what you can do here, so it is part of identity.
    # A checkout with Continue disabled is not the same state as one where it
    # is clickable, and a planner that conflates them emits unwalkable paths.
    if element.get("disabled"):
        parts.append("disabled")
    if element.get("readonly"):
        parts.append("readonly")

    return "|".join(parts)


def _bucket(count: int) -> str:
    """Collapse repetition counts so list length stops mattering.

    A collection page renders however many rows the data happens to have. If
    the count entered the fingerprint, creating one project would change the
    identity of the projects page and the graph would never converge.
    """
    if count <= 1:
        return "1"
    if count <= 3:
        return "2-3"
    return "many"


# --------------------------------------------------------------------------
# State fingerprint
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fingerprint:
    """A state identity, plus the parts it was derived from.

    The components are kept so a drifting fingerprint can be debugged by
    diffing two of these, rather than by staring at two hashes that differ.
    """

    id: str
    url_template: str
    signature: tuple[str, ...]

    def explain(self) -> str:
        lines = [f"{self.id}  {self.url_template}"]
        lines.extend(f"    {s}" for s in self.signature)
        return "\n".join(lines)


def fingerprint(snapshot: dict, *, keep_query_keys: bool = True) -> Fingerprint:
    """Reduce a `page_inspector` snapshot to a stable state identity."""
    template = url_template(snapshot.get("url", ""), keep_query_keys=keep_query_keys)

    counts: dict[str, int] = {}
    for element in snapshot.get("elements", []):
        signature = element_signature(element)
        counts[signature] = counts.get(signature, 0) + 1

    # Sorted, so DOM order never affects identity. A nav rendered before or
    # after main content is the same page.
    signature = tuple(
        f"{sig}x{_bucket(count)}" for sig, count in sorted(counts.items())
    )

    digest = hashlib.sha256(
        ("\n".join((template,) + signature)).encode("utf-8")
    ).hexdigest()

    return Fingerprint(id=f"s_{digest[:10]}", url_template=template, signature=signature)


def diff(left: Fingerprint, right: Fingerprint) -> dict:
    """What changed between two fingerprints.

    Used both for debugging instability and, once the crawl is running, as the
    raw material for an edge's recorded effect.
    """
    left_set, right_set = set(left.signature), set(right.signature)
    return {
        "url_template": (
            None
            if left.url_template == right.url_template
            else [left.url_template, right.url_template]
        ),
        "added": sorted(right_set - left_set),
        "removed": sorted(left_set - right_set),
    }
