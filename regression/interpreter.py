"""Explain a set of findings: what probably broke, and what it means.

The differ produces facts. Twenty of them, mostly neutral, each true and none
of them an answer to the question someone actually has, which is "did this
build break anything and should I care". Turning a list of differences into
that answer is the one job in this pipeline a language model is genuinely
better at than a rule, because it requires reading several unrelated facts as
one story:

    DELETION_COMPLETENESS went from holds to violated
    route /projects/<id> is still exposed
    .card p on /projects matches '3 projects', was '2 projects'

Those are three findings. They are one defect: the delete stopped removing
the resource, so it is still served and still counted. No rule in this
codebase can make that connection, and a person reading a flat list has to
make it themselves every time.

Boundaries, which matter more here than anywhere else in the system:

  the model never decides a verdict.  Whether `sum(line_totals) ==
  order_total` held was settled deterministically by the knowledge engine
  before this module ran, and nothing here can overturn it. The model groups
  and explains findings; it does not produce them.

  the model never invents a finding.  Every interpretation must cite the
  subjects it covers, and `validate_interpretations` drops any that cite
  something the differ did not report. An explanation of a defect that does
  not exist is the one output worse than no explanation at all.

  "intentional" is a reading, not a dismissal.  A removed route may well be a
  deliberate deletion, and saying so is useful. It is still reported, still
  counted, and still shown — the model's assessment annotates a finding and
  never filters one out.

Defaults to OpenAI — `BLINDSPOT_LLM_PROVIDER` still wins when set, and
`--provider` beats both.
"""

from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from regression.findings import Direction, FindingSet
from semantic.classifier import LLMBackend, backend_for
from semantic.schemas import strict_schema

DEFAULT_PROVIDER = "openai"

# Beyond this, the prompt stops being a story and starts being a database
# dump. Findings are ranked before truncation, so what survives is the
# regressions and the highest-severity changes.
MAX_FINDINGS = 40


class Assessment(str, Enum):
    """How a group of findings most likely reads."""

    REGRESSION = "regression"              # something broke
    INTENTIONAL = "intentional_change"     # looks like deliberate work
    INSTABILITY = "instability"            # test or crawler noise, not the app
    UNCLEAR = "unclear"                    # not enough evidence to say


class Interpretation(BaseModel):
    """One story assembled from one or more findings."""

    model_config = ConfigDict(extra="forbid")

    subjects: list[str] = Field(
        description="The `subject` values of the findings this covers, copied "
        "exactly. Every one must appear in the findings you were given."
    )
    assessment: Assessment
    headline: str = Field(
        max_length=120,
        description="One line a reader skims. State the effect, not the "
        "mechanism: 'deleting a project no longer removes it'.",
    )
    explanation: str = Field(
        max_length=700,
        description="Why these findings go together and what they indicate. "
        "Reference the evidence you were given; do not speculate beyond it.",
    )
    user_impact: str = Field(
        max_length=300,
        description="What someone using the application would experience. "
        "Empty string when there is no user-visible impact.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0-1, how strongly the findings support this reading. "
        "This is confidence in the EXPLANATION, never in the verdicts — "
        "those were decided deterministically before you saw them.",
    )


class InterpretationSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretations: list[Interpretation]
    overall: str = Field(
        max_length=400,
        description="Two or three sentences on what this build changed, "
        "leading with anything that broke.",
    )


class RejectedInterpretation(BaseModel):
    interpretation: Interpretation
    reason: str


class InterpretationResult(BaseModel):
    model: str
    interpretations: list[Interpretation]
    rejected: list[RejectedInterpretation]
    overall: str


SYSTEM_PROMPT = """\
You explain the results of an automated regression comparison of a web \
application. Two builds were crawled and the same tests were replayed against \
both; you are given the differences.

Each finding has a kind, a direction, a subject, and supporting detail:

  direction  `regressed` means something got worse — an invariant that held \
now fails, a test that ran now cannot, a route or control that disappeared. \
`fixed` means the opposite. `changed` means different and neither.
  kind       `verdict` an invariant's status moved. `observation` the numbers \
behind it moved. `run` a test errored. `missing` a test was not run at all. \
`structure` a route appeared or vanished. `ui` controls or rendered values \
moved.

YOUR JOB:
  - GROUP findings that are the same underlying change. Several findings \
usually describe one defect from different angles — a failed invariant, the \
observation behind it, and the rendered value that moved. One interpretation \
covering all three is far more useful than three covering one each.
  - ASSESS each group: regression, intentional_change, instability, or \
unclear.
  - EXPLAIN what it means, and what a user would experience.

WHAT YOU MUST NOT DO:
  - Do not decide whether an invariant held. That was computed \
deterministically before you saw it. If a finding says a verdict went from \
holds to violated, that is a fact; your job is to explain it, not to \
re-adjudicate it.
  - Do not invent findings. Every `subjects` entry must be the exact \
`subject` string of a finding you were given. If you cannot support a claim \
from the evidence, do not make it.
  - Do not speculate about code you cannot see. You have observations, not \
source. "The handler probably skips the assignment" is speculation; "the \
resource is still served at its own URL after deletion" is evidence.

CALIBRATION:
  - A `regressed` verdict finding is the strongest signal here and should \
almost always be assessed `regression`.
  - A route or control that vanished with no failing invariant is often \
`intentional_change` — a removed feature. Say so, and say what it was.
  - Reworded labels, reordered controls, and values that moved while their \
invariant still holds are usually `intentional_change` or `unclear`, not \
regressions.
  - Findings that contradict each other, or a test that errored with no \
other trace, may be `instability` — the crawler or the test, not the \
application. Flag it as such rather than reporting a defect you cannot \
substantiate.

Write for someone who knows the application and has not seen this diff. Lead \
with what broke.\
"""


def build_user_prompt(findings: FindingSet, *, limit: int = MAX_FINDINGS) -> str:
    """Render the findings as the user turn, most important first."""
    ranked = findings.ranked()
    counts = findings.counts()

    lines = [
        "Here are the differences between the baseline and this build.",
        "",
        f"Summary: {counts['regressed']} regressed, {counts['fixed']} fixed, "
        f"{counts['changed']} changed ({counts['total']} findings).",
        "",
    ]

    for finding in ranked[:limit]:
        lines.append(
            f"- [{finding.direction.value}/{finding.kind.value}] "
            f"subject={finding.subject!r}"
            + (f" invariant={finding.invariant}" if finding.invariant else "")
            + (f" severity={finding.severity}" if finding.severity else "")
        )
        lines.append(f"    {finding.summary}")
        if finding.detail:
            lines.append(f"    detail: {finding.detail}")

    if len(ranked) > limit:
        lines.append(
            f"\n({len(ranked) - limit} further lower-priority findings omitted.)"
        )

    lines.append(
        "\nGroup these into the underlying changes, assess each, and explain "
        "what this build did."
    )
    return "\n".join(lines)


def validate_interpretations(
    interpretations, findings: FindingSet
) -> tuple[list[Interpretation], list[RejectedInterpretation]]:
    """Drop interpretations that cite findings the differ never reported.

    The schema guarantees the shape; it cannot guarantee that `subjects`
    names anything real, which is exactly where a confident fabrication
    lands. An interpretation citing one real subject and one invented one is
    rejected whole rather than trimmed — the invented half is usually load
    bearing in the story it tells.
    """
    known = {f.subject for f in findings}
    kept: list[Interpretation] = []
    rejected: list[RejectedInterpretation] = []

    for interpretation in interpretations:
        if not interpretation.subjects:
            rejected.append(RejectedInterpretation(
                interpretation=interpretation,
                reason="cites no findings",
            ))
            continue
        unknown = [s for s in interpretation.subjects if s not in known]
        if unknown:
            rejected.append(RejectedInterpretation(
                interpretation=interpretation,
                reason=(
                    f"cites subject(s) not present in the findings: "
                    f"{', '.join(repr(u) for u in unknown[:3])}"
                ),
            ))
            continue
        kept.append(interpretation)

    kept.sort(key=lambda i: (i.assessment is not Assessment.REGRESSION,
                             -i.confidence))
    return kept, rejected


def interpret(
    findings: FindingSet,
    *,
    backend: LLMBackend | None = None,
) -> InterpretationResult:
    """Explain a set of findings.

    Returns an empty result for an empty finding set without calling a model:
    there is nothing to explain, and "no differences" is better said by the
    reporter than paid for.
    """
    if not len(findings):
        return InterpretationResult(
            model="(not called)",
            interpretations=[],
            rejected=[],
            overall="No differences were found between the baseline and this build.",
        )

    backend = backend or backend_for(default_provider=DEFAULT_PROVIDER)

    raw = backend.complete_json(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(findings),
        schema=strict_schema(InterpretationSet),
    )

    parsed = InterpretationSet.model_validate_json(raw)
    kept, rejected = validate_interpretations(parsed.interpretations, findings)

    return InterpretationResult(
        model=backend.model,
        interpretations=kept,
        rejected=rejected,
        overall=parsed.overall,
    )


def fallback(findings: FindingSet) -> InterpretationResult:
    """A deterministic stand-in for when no model is available.

    Groups nothing and explains nothing — it restates the regressions. The
    point is that a missing API key degrades the report rather than failing
    the run: the findings were computed without a model and are the actual
    evidence, so they remain useful on their own.
    """
    regressions = findings.regressions
    if not regressions:
        overall = (
            f"No regressions found. {findings.counts()['changed']} neutral "
            f"change(s) were recorded."
        )
    else:
        overall = (
            f"{len(regressions)} regression(s) found, un-interpreted "
            f"(no LLM backend available)."
        )
    return InterpretationResult(
        model="(none)",
        interpretations=[],
        rejected=[],
        overall=overall,
    )


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    from regression.findings import Finding

    parser = argparse.ArgumentParser(prog="blindspot-interpret")
    parser.add_argument("findings", type=Path,
                        help="JSON produced by FindingSet.to_dict()")
    parser.add_argument("--provider", help=f"default: {DEFAULT_PROVIDER}")
    parser.add_argument("--model", help="provider-specific model id")
    parser.add_argument("--prompt-only", action="store_true")
    args = parser.parse_args()

    blob = json.loads(args.findings.read_text())
    loaded = FindingSet([Finding.from_dict(f) for f in blob.get("findings", [])])

    if args.prompt_only:
        print(build_user_prompt(loaded))
        raise SystemExit(0)

    result = interpret(
        loaded,
        backend=backend_for(args.provider, args.model,
                            default_provider=DEFAULT_PROVIDER),
    )
    print(result.model_dump_json(indent=2))
