"""Terminal and JSON output for a regression comparison.

A regression report has a different job from a scan report, and the shape
follows from it. A scan answers "does this application hold its invariants";
the reader starts from nothing and wants every result. A regression answers
"did this build break anything"; the reader already believes the application
works and wants the exception. Most of a regression run's output is therefore
noise to its reader by construction, and the formatting exists almost
entirely to keep the few findings that matter from being buried in it.

So: regressions first and in full, the model's reading of them beside them,
fixes next because confirming one is why half of these runs happen, and the
neutral changes collapsed to counts with a pointer to the saved record.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from regression.findings import Direction, FindingSet, Kind
from reporting.reporter import _Style, _supports_color

RUNS_DIR = Path("runs")

# Neutral findings are listed up to here and counted past it. A build that
# reworded forty labels should not push its one failing invariant off the
# screen.
_MAX_NEUTRAL = 8


class RegressionReporter:
    """Streams a regression comparison to the terminal."""

    def __init__(self, stream=None, color: bool | None = None):
        import sys

        self.out = stream or sys.stdout
        self.s = _Style(_supports_color(self.out) if color is None else color)

    def _p(self, text: str = "") -> None:
        print(text, file=self.out)

    # -- progress ----------------------------------------------------------

    def header(self, baseline_dir, url: str, recorded_at: str = "") -> None:
        self._p()
        self._p(self.s.bold("BlindSpot 0.2 — regression check"))
        self._p(self.s.dim(f"baseline: {baseline_dir}"
                           + (f"  (recorded {recorded_at})" if recorded_at else "")))
        self._p(self.s.dim(f"against:  {url}"))
        self._p()

    def replaying(self, count: int) -> None:
        self._p(f"Replaying {count} recorded test(s) against this build...")
        self._p()

    def replayed(self, record, verdict) -> None:
        mark = {
            "holds": self.s.green("holds"),
            "violated": self.s.red("violated"),
            "inconclusive": self.s.blue("inconclusive"),
        }.get(verdict.status.value, verdict.status.value)
        self._p(f"  {record.test_id}  {record.invariant:28}  {mark}")

    def comparing(self) -> None:
        self._p()
        self._p("Comparing against the baseline...")
        self._p()

    # -- findings ----------------------------------------------------------

    def _finding(self, finding, colour) -> None:
        tag = f"[{finding.kind.value}]"
        self._p(f"  {colour(finding.summary)}")
        meta = "    " + tag
        if finding.invariant:
            meta += f" {finding.invariant}"
        if finding.severity:
            meta += f" severity={finding.severity}"
        self._p(self.s.dim(meta))
        if finding.detail:
            self._p(self.s.dim(f"    {finding.detail}"))

    def findings(self, found: FindingSet) -> None:
        counts = found.counts()

        if not len(found):
            self._p(self.s.green("No differences found."))
            self._p(self.s.dim(
                "  The same tests reached the same verdicts from the same "
                "observations,\n  and the application exposes the same "
                "structure and surface."
            ))
            self._p()
            return

        regressions = found.of_direction(Direction.REGRESSED)
        if regressions:
            self._p(self.s.red(f"REGRESSED ({len(regressions)})"))
            self._p()
            for finding in regressions:
                self._finding(finding, self.s.red)
                self._p()
        else:
            self._p(self.s.green("Nothing regressed."))
            self._p()

        fixed = found.of_direction(Direction.FIXED)
        if fixed:
            self._p(self.s.green(f"FIXED ({len(fixed)})"))
            self._p()
            for finding in fixed:
                self._finding(finding, self.s.green)
                self._p()

        changed = found.of_direction(Direction.CHANGED)
        if changed:
            self._p(self.s.bold(f"CHANGED ({len(changed)})"))
            self._p()
            for finding in changed[:_MAX_NEUTRAL]:
                self._finding(finding, lambda t: t)
            if len(changed) > _MAX_NEUTRAL:
                by_kind: dict[str, int] = {}
                for finding in changed[_MAX_NEUTRAL:]:
                    by_kind[finding.kind.value] = by_kind.get(finding.kind.value, 0) + 1
                tail = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
                self._p(self.s.dim(
                    f"    and {len(changed) - _MAX_NEUTRAL} more ({tail}) — "
                    f"see the saved record"
                ))
            self._p()

    # -- interpretation ----------------------------------------------------

    def interpretation(self, result) -> None:
        if result.interpretations:
            self._p(self.s.bold(f"What this build changed  "
                                + self.s.dim(f"({result.model})")))
            self._p()
            for item in result.interpretations:
                label = {
                    "regression": self.s.red("REGRESSION"),
                    "intentional_change": self.s.blue("LIKELY INTENTIONAL"),
                    "instability": self.s.amber("POSSIBLE INSTABILITY"),
                    "unclear": self.s.amber("UNCLEAR"),
                }.get(item.assessment.value, item.assessment.value)
                self._p(f"  {label}  {self.s.bold(item.headline)}")
                self._p(f"    {item.explanation}")
                if item.user_impact:
                    self._p(f"    {self.s.dim('Impact:')} {item.user_impact}")
                self._p(self.s.dim(
                    f"    from: {', '.join(item.subjects)}  "
                    f"(confidence {item.confidence:.2f})"
                ))
                self._p()

        for rejected in result.rejected:
            self._p(self.s.dim(f"  discarded interpretation: {rejected.reason}"))
        if result.rejected:
            self._p()

        if result.overall:
            self._p(self.s.bold("Overall"))
            self._p(f"  {result.overall}")
            self._p()

    def summary(self, found: FindingSet, path: Path | None,
                baseline_saved: Path | None = None) -> None:
        counts = found.counts()
        self._p(self.s.bold("Summary"))
        self._p(
            f"  {counts['regressed']} regressed, {counts['fixed']} fixed, "
            f"{counts['changed']} changed"
        )
        if path:
            self._p(f"  Report: {path}")
        if baseline_saved:
            self._p(f"  New baseline: {baseline_saved}")
        self._p()
        if counts["regressed"]:
            self._p(self.s.amber(
                "  BlindSpot found deterministic differences against the "
                "recorded baseline.\n  Whether each is a defect is a "
                "judgement call."
            ))
            self._p()


# --------------------------------------------------------------------------
# Machine-readable record
# --------------------------------------------------------------------------


def build_regression_record(
    *,
    baseline_dir,
    url: str,
    baseline,
    current,
    found: FindingSet,
    interpretation=None,
) -> dict:
    """Assemble the JSON record for one regression comparison."""
    return {
        "blindspot_version": "0.2",
        "mode": "regression",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": {
            "directory": str(baseline_dir),
            "recorded_at": baseline.created_at,
            "base_url": baseline.base_url,
            "model": baseline.model,
            "counts": baseline.manifest()["counts"],
        },
        "current": {
            "url": url,
            "base_url": current.base_url,
            "counts": current.manifest()["counts"],
        },
        **found.to_dict(),
        "interpretation": (
            {
                "model": interpretation.model,
                "overall": interpretation.overall,
                "interpretations": [
                    item.model_dump(mode="json")
                    for item in interpretation.interpretations
                ],
                "rejected": [
                    {"reason": r.reason,
                     "interpretation": r.interpretation.model_dump(mode="json")}
                    for r in interpretation.rejected
                ],
            }
            if interpretation is not None
            else None
        ),
    }


def write_regression_record(record: dict, directory: Path | str = RUNS_DIR) -> Path:
    """Write `runs/regressions/<timestamp>.json`.

    Not alongside the scan records: `reporting.dashboard` globs `runs/*.json`
    and reads every hit as a persistence run, so a regression report dropped
    in there is picked up as a malformed scan.
    """
    directory = Path(directory) / "regressions"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    path = directory / f"{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


def exit_code(found: FindingSet) -> int:
    """0 when nothing regressed, 1 when something did.

    Neutral changes do not fail a run. A build that added a route has not
    broken anything, and a check that fails on every difference is one that
    gets switched off.
    """
    return 1 if found.regressions else 0
