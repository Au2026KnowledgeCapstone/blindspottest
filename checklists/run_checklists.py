
"""
Coordinate checklist resolution and graph verification in Blindspot.

This module provides the command-line entry point and orchestration
layer for running registered QA checklists against a discovered
application graph.

It connects three existing components:

- ChecklistBank: Supplies validated checklist specifications.
- AppGraph: Supplies the application's discovered states and actions.
- semantics.checklist_resolver: Interprets checklist requirements and
  verifies their connectivity against the discovered graph.

The runner does not implement its own semantic interpretation or
graph path-finding logic. It delegates those responsibilities to
the existing modules.

The pipeline works in four stages.

- CHECKLIST SELECTION

The runner retrieves checklist definitions from ChecklistBank.

A user can select an individual checklist by its identifier or
execute multiple checklists from the registered library.

When running multiple checklists, optional filters allow selection
by category, priority, or tag.

- CHECKLIST RESOLUTION

Each selected Checklist is validated and converted into a natural-
language specification using Checklist.to_prompt().

The resulting specification is passed to semantics.checklist_resolver.

The resolver interprets the requested journey, matches its
requirements against existing graph states and actions, and checks
whether the required checkpoints are connected in order.

The runner does not independently decide which graph objects
satisfy a checklist requirement.

- RESULT COLLECTION AND CLASSIFICATION

The runner collects the resolution result and associates it with
the original checklist metadata.

Each result includes the checklist identifier, title, category,
priority, resolution status, graph path, matched steps, goal
conditions, unresolved requirements, and explanation.

Resolution outcomes distinguish between found, not_found, and
inconclusive.

A found result means that the required checkpoints are connected
in the discovered graph. It does not prove that the corresponding
actions are executable or that the application behaves correctly.

- REPORTING AND OUTPUT

The runner provides human-readable reporting for individual
checklists and aggregate summaries for multiple checklist runs.

Results can also be exported as structured JSON for integration
with other Blindspot components, reporting systems, or future
regression-testing infrastructure.

- HANDOFF TO DOWNSTREAM TESTING

The runner currently performs checklist resolution and graph
verification only.

It does not instantiate executable test cases, populate browser
inputs, evaluate action preconditions, execute application
interactions, or verify goal conditions.

Those responsibilities belong to the downstream knowledge and
execution pipelines.

The runner's purpose is to coordinate existing components and
produce a consistent representation of which requested application
journeys are supported by the discovered graph.

It provides the connection between the stored checklist library
and Blindspot's graph-based semantic verification architecture.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from checklists.bank import ChecklistBank
from checklists.models import Checklist

from discovery.graph import AppGraph
from semantic.checklist_resolver import check_flow
from semantic.classifier import LLMBackend


def run_checklist(
    checklist: Checklist,
    graph: AppGraph,
    *,
    backend: LLMBackend | None = None,
    start_state: str | None = None,
) -> dict[str, Any]:
    """
    Resolve and verify one checklist against a discovered AppGraph.

    The checklist is converted into a natural-language specification
    and passed to the semantic resolver.

    This function does not execute the resulting journey.

    Args:
        checklist: Checklist specification to process.
        graph: Discovered application graph.
        backend: Optional configured LLM backend.
        start_state: Optional starting graph state.

    Returns:
        Structured result associating checklist metadata with
        its graph resolution and verification results.
    """

    checklist.validate()

    result = check_flow(
        checklist.to_prompt(),
        graph,
        backend=backend,
        start_state=start_state,
    )

    return {
        "checklist_id": checklist.id,
        "checklist_title": checklist.title,
        "checklist_version": checklist.version,
        "category": checklist.category,
        "priority": checklist.priority,

        "resolution_status": result["status"],
        "graph_path_exists": result["exists"],

        "feasibility_status": result["feasibility_status"],
        "execution_status": "not_started",
        "verification_status": "not_started",

        "path": result["path"],
        "matched_steps": result["matched_steps"],
        "missing_step": result["missing_step"],

        "goal_conditions": result["goal_conditions"],
        "unresolved": result["unresolved"],

        "explanation": result["explanation"],
    }


def select_checklists(
    bank: ChecklistBank,
    *,
    category: str | None = None,
    priority: str | None = None,
    tag: str | None = None,
) -> list[Checklist]:
    """
    Select checklists from the bank using optional filters.

    Multiple filters are combined using AND logic.

    Args:
        bank: Checklist repository.
        category: Optional checklist category.
        priority: Optional priority level.
        tag: Optional checklist tag.

    Returns:
        List of matching checklist definitions.
    """

    checklists = bank.list_all()

    if category:
        checklists = [
            item
            for item in checklists
            if item.category == category
        ]

    if priority:
        checklists = [
            item
            for item in checklists
            if item.priority == priority
        ]

    if tag:
        checklists = [
            item
            for item in checklists
            if tag in item.tags
        ]

    return checklists


def run_all_checklists(
    bank: ChecklistBank,
    graph: AppGraph,
    *,
    category: str | None = None,
    priority: str | None = None,
    tag: str | None = None,
    backend: LLMBackend | None = None,
) -> list[dict[str, Any]]:
    """
    Run all checklists matching the supplied filters.

    Each checklist is independently resolved against the same
    discovered application graph.

    Args:
        bank: Checklist repository.
        graph: Discovered application graph.
        category: Optional category filter.
        priority: Optional priority filter.
        tag: Optional tag filter.
        backend: Optional shared LLM backend.

    Returns:
        List of structured checklist results.
    """

    checklists = select_checklists(
        bank,
        category=category,
        priority=priority,
        tag=tag,
    )

    results = []

    for checklist in checklists:
        results.append(
            run_checklist(
                checklist,
                graph,
                backend=backend,
            )
        )

    return results


def summarize_results(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Calculate aggregate graph resolution statistics.

    Counts found, not_found, and inconclusive results separately.

    These statistics describe graph resolution outcomes rather than
    runtime test execution results.
    """

    counts = {
        "found": 0,
        "not_found": 0,
        "inconclusive": 0,
    }

    for result in results:
        status = result["resolution_status"]

        if status in counts:
            counts[status] += 1

    return {
        "total": len(results),
        **counts,
    }


def print_results(
    results: list[dict[str, Any]],
) -> None:
    """
    Print a human-readable report of checklist resolution results.

    The report distinguishes graph connectivity from runtime
    execution and behavioral verification.
    """

    for result in results:

        print("\n" + "=" * 65)

        print(
            f"{result['checklist_id']} - "
            f"{result['checklist_title']}"
        )

        print(
            f"Resolution: {result['resolution_status']}"
        )

        print(
            f"Graph path exists: {result['graph_path_exists']}"
        )

        print(
            f"Feasibility: {result['feasibility_status']}"
        )

        print(
            f"Execution: {result['execution_status']}"
        )

        print(
            f"Runtime verification: "
            f"{result['verification_status']}"
        )

        if result["matched_steps"]:
            print("\nMatched checkpoints:")

            for step in result["matched_steps"]:
                print(f"  [MATCH] {step}")

        if result["missing_step"]:
            print(
                f"\nMissing checkpoint: "
                f"{result['missing_step']}"
            )

        if result["unresolved"]:
            print("\nUnresolved requirements:")

            for item in result["unresolved"]:
                print(
                    f"  - {item['description']}: "
                    f"{item['reason']}"
                )

        if result["goal_conditions"]:
            print(
                "\nGoal conditions "
                "(not yet runtime verified):"
            )

            for condition in result["goal_conditions"]:
                print(f"  - {condition}")

        print(
            f"\nExplanation: {result['explanation']}"
        )

    summary = summarize_results(results)

    print("\n" + "=" * 65)
    print("CHECKLIST SUMMARY")
    print("=" * 65)

    for name, count in summary.items():
        print(f"{name}: {count}")


def main() -> None:
    """
    Execute checklist resolution from the command line.

    Supports individual checklist execution and filtered execution
    of the registered checklist library.

    Results can be printed as a readable report or exported as JSON.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Run Blindspot checklist graph verification."
        )
    )

    parser.add_argument(
        "--graph",
        required=True,
        help="Path to discovered AppGraph JSON.",
    )

    parser.add_argument(
        "--checklist",
        help="Checklist ID to run.",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all registered checklists.",
    )

    parser.add_argument(
        "--category",
        help="Filter checklists by category.",
    )

    parser.add_argument(
        "--priority",
        choices=["critical", "high", "medium", "low"],
        help="Filter checklists by priority.",
    )

    parser.add_argument(
        "--tag",
        help="Filter checklists by tag.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON results.",
    )

    args = parser.parse_args()

    if not args.checklist and not args.all:
        parser.error(
            "Provide --checklist ID or --all."
        )

    if args.checklist and args.all:
        parser.error(
            "Use --checklist or --all, not both."
        )

    if args.checklist and any(
        (args.category, args.priority, args.tag)
    ):
        parser.error(
            "Category, priority, and tag filters "
            "are only available with --all."
        )

    # Load checklist definitions and discovered graph.
    bank = ChecklistBank()
    graph = AppGraph.load(args.graph)

    # Run an individual checklist or the selected collection.
    if args.checklist:

        checklist = bank.get(args.checklist)

        results = [
            run_checklist(
                checklist,
                graph,
            )
        ]

    else:

        results = run_all_checklists(
            bank,
            graph,
            category=args.category,
            priority=args.priority,
            tag=args.tag,
        )

    output = {
        "summary": summarize_results(results),
        "results": results,
    }

    if args.json:

        print(
            json.dumps(
                output,
                indent=2,
                ensure_ascii=False,
            )
        )

    else:

        print_results(results)


if __name__ == "__main__":
    main()