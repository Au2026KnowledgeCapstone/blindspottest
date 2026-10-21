"""Decide what state a page is in, including when the page will not say.

`discovery.fingerprint` assumes a page has one identity. Most do. A page with
an autoplaying carousel does not: react-slick rotates its window every few
hundred milliseconds, products slide in and out of the aria-visible set, and
the number of copies of a given link on the page changes with it. Since the
count is part of the signature, the identity changes too — a new state id
every rotation, from a page nobody touched.

What that costs the crawl is not cosmetic. Each phantom identity is a new
state, so every affordance on the page is queued and fired again under it;
every one of those needs the application put back, so the replay cost is paid
again too; and the graph ends up with several nodes that are one page, edges
scattered between them, and a closure claim that means less than it says.

The fix has to start by admitting the problem: a page that renders on a timer
has no single identity, and no amount of waiting produces one. `--settle`
cannot help, because there is nothing to settle *to*. What there is, is a
part of the page that holds still — the header, the nav, the form, the
controls that actually determine what you can do here — and a part that does
not. Identity belongs to the first part.

So: read the page several times across a window wide enough to span a
typical animation, and compare. If every read agrees, nothing has changed and
the cost was one extra snapshot. If they disagree, the signatures that moved
are recorded as volatile for that URL template and excluded from the identity
of that page from then on. The page gets one id, derived from the part of it
that is actually stable, and the crawl is told so rather than left to
discover it as duplicate states.

Two things this is careful not to do:

* Not guess. A signature is excluded only after being *observed* to move.
  Dropping anything that merely looks dynamic would quietly weaken identity
  on every page in the application to fix a problem on one of them.
* Not hide it. Volatility is recorded on the graph and reported, because
  "this page has no stable identity" is a finding about the application, and
  a map that silently papered over it would be making the same mistake in
  the other direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from discovery.fingerprint import Fingerprint, element_counts, fingerprint, url_template

# Reads taken when a page is first seen, and the gap between them. The window
# is (reads - 1) x gap, so the default spans one second — wide enough for the
# 800ms carousel that motivated this, and for most of the ones that did not.
#
# Paid once per URL template rather than once per snapshot: the question
# "does this page hold still" is a property of the page, and asking it again
# on every visit would triple the cost of the crawl to re-learn a known
# answer.
DEFAULT_READS = 3
DEFAULT_GAP_MS = 500

# How many times a URL template may pass the check before it is believed and
# stops being re-checked. Without a cap the cost is per *state* rather than
# per template, and a parameterised route pays it once for every instance —
# ten product pages, ten second-long windows, to re-establish a fact already
# established nine times. Three is enough to span an animation that happened
# to be out of phase on the first look.
DEFAULT_TRUST_AFTER = 3


@dataclass
class StateReader:
    """Identifies pages, and remembers which ones cannot identify themselves.

    `identify` is the cheap path and is what the crawl uses everywhere it
    needs to know where it is. `settle` is the expensive one, called only
    when the answer is about to create something — a new URL template, or a
    second identity for a template that already has one.
    """

    reads: int = DEFAULT_READS
    gap_ms: int = DEFAULT_GAP_MS
    trust_after: int = DEFAULT_TRUST_AFTER
    strip_prefix: str = ""
    on_event: Callable[[str], None] | None = None

    # url template -> the element signatures observed to move on it.
    volatile: dict[str, frozenset[str]] = field(default_factory=dict)
    # url template -> how many times it has been checked and held still.
    _clean: dict[str, int] = field(default_factory=dict, repr=False)
    # url template -> how many times its volatile set has been widened.
    _relearned: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def enabled(self) -> bool:
        return self.reads > 1

    def should_settle(self, template: str, *, novel: bool) -> bool:
        """Whether this read is worth paying the multi-read check for.

        Yes when the identity is about to create something — a template
        never seen, or a second identity for a template that already has a
        state, which is what a timer-driven page looks like from here. No
        once the template has held still often enough to be believed.

        A template already known to move is still re-checked when it
        produces an identity we have not seen. The reads are taken over one
        window, and a window only catches what moved inside it: a carousel
        with ten slides may show two of them changing in the first second
        and the rest later. A novel identity on a page already known to
        drift is that — a signature the first look missed — and refusing to
        look again would leave exactly the duplicate state this exists to
        prevent. Both paths are capped so a page that is dynamic all the way
        down cannot spend the whole crawl being measured.
        """
        if not self.enabled or not novel:
            return False
        if self.is_volatile(template):
            return self._relearned.get(template, 0) < self.trust_after
        return self._clean.get(template, 0) < self.trust_after

    # -- identifying -------------------------------------------------------

    def template_of(self, snapshot: dict) -> str:
        return url_template(snapshot.get("identity_url", snapshot.get("url", "")), strip_prefix=self.strip_prefix)

    def identify(self, snapshot: dict) -> Fingerprint:
        """One snapshot to an identity, honouring what has been learned."""
        template = self.template_of(snapshot)
        return fingerprint(
            snapshot,
            strip_prefix=self.strip_prefix,
            ignore=self.volatile.get(template, frozenset()),
        )

    def is_volatile(self, template: str) -> bool:
        return template in self.volatile

    # -- learning ----------------------------------------------------------

    def settle(
        self, take: Callable[[], dict], pause: Callable[[int], None], snapshot: dict
    ) -> tuple[dict, Fingerprint]:
        """Re-read until the identity repeats, or learn that it never will.

        `snapshot` is the read the caller already has, so the first of the
        reads is free. Returns the snapshot to record and its identity.

        A read that lands on a different URL template is navigation finishing
        underneath us, not animation. Re-reading through it would compare two
        different pages and declare both unstable, so it stops and hands back
        what it has; the caller will see the page again.
        """
        if not self.enabled:
            return snapshot, self.identify(snapshot)

        template = self.template_of(snapshot)
        seen = [snapshot]
        for _ in range(self.reads - 1):
            pause(self.gap_ms)
            later = take()
            if self.template_of(later) != template:
                return snapshot, self.identify(snapshot)
            seen.append(later)

        known = self.volatile.get(template, frozenset())
        moved = _volatile_signatures(seen) | known
        if moved == known:
            # Nothing new. For a template that was already clean that is a
            # vote of confidence; for one already known to move it means the
            # novel identity came from somewhere these reads cannot see, and
            # looking harder is not obviously going to help.
            counter = self._relearned if known else self._clean
            counter[template] = counter.get(template, 0) + 1
            return snapshot, self.identify(snapshot)

        self.volatile[template] = moved
        if known:
            self._relearned[template] = self._relearned.get(template, 0) + 1
            self._say(
                f"  [unstable] {template} moved again — "
                f"{len(moved - known)} further signature(s) excluded"
            )
        else:
            self._say(
                f"  [unstable] {template} renders on a timer — "
                f"{len(moved)} signature(s) excluded from its identity"
            )
        for sig in sorted(moved - known)[:4]:
            self._say(f"             {sig}")
        if len(moved - known) > 4:
            self._say(f"             ... and {len(moved - known) - 4} more")

        # The last read, because it is the one closest to what the page looks
        # like now. With the volatile signatures excluded they all identify
        # the same anyway; the choice only affects the representative
        # snapshot the projections render.
        return seen[-1], self.identify(seen[-1])

    def _say(self, message: str) -> None:
        if self.on_event:
            self.on_event(message)


def _volatile_signatures(snapshots: list[dict]) -> frozenset[str]:
    """Element signatures whose count was not the same in every read.

    Keyed on the signature rather than the signature-plus-count, so a link
    seen once as one copy and once as two is excluded however many copies a
    later visit happens to find. Keying on the count instead would learn
    "one copy of this is volatile" and then be surprised by three.
    """
    counted = [element_counts(s) for s in snapshots]
    everything: set[str] = set()
    for counts in counted:
        everything |= counts.keys()

    return frozenset(
        sig for sig in everything
        if len({counts.get(sig, 0) for counts in counted}) > 1
    )
