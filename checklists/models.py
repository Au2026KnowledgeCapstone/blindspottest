
"""
Define and validate application-independent QA checklist specifications.

This module provides the Checklist data model used throughout Blindspot
to represent user-defined application testing requirements.

A checklist describes a desired application behavior independently of
the application's implementation or discovered navigation structure.

It does not contain application-specific state identifiers, browser
selectors, action references, or predefined navigation paths. Those
are resolved later against the application's discovered AppGraph.

A checklist contains the following information:

- Identity: A stable identifier, title, category, and description.
- Priority: The relative importance of the requirement, ranging from
  critical to low.
- Version: The version of the checklist specification.
- Status: Whether the checklist is active, a draft, or deprecated.
- Test type: The general testing classification, such as positive,
  negative, boundary, or persistence.
- Preconditions: Conditions that should be established before the
  requested journey begins.
- Steps: An ordered description of the actions and navigation
  requirements that constitute the desired user journey.
- Goal conditions: Observable outcomes that should hold when the
  requested journey is complete.
- Tags: Additional metadata used to categorize and retrieve checklists.

The module is responsible for validating checklist definitions before
they enter the rest of the testing pipeline.

Validation ensures that required descriptive fields are populated,
priorities and statuses use supported values, versions are positive
integers, and collections contain valid non-empty strings.

The from_dict() method constructs a Checklist from JSON-compatible
dictionary data and validates the resulting object.

The to_dict() method produces a validated, JSON-serializable
representation of the checklist.

The to_prompt() method translates the structured specification into
a readable natural-language representation for the semantic checklist
resolver.

This formatting preserves the distinction between preconditions,
ordered journey steps, and expected goal conditions. These represent
different testing requirements and must not be treated as
interchangeable navigation checkpoints.

The model does not determine whether a requested journey exists in an
application, whether its actions are executable, or whether its goal
conditions are satisfied.

Its responsibility is to provide a consistent, validated representation
of what the user wants Blindspot to test.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Checklist:
    """Application-independent QA checklist specification."""

    id: str
    title: str
    category: str
    description: str
    steps: list[str]
    goal_conditions: list[str]

    priority: str = "medium"
    preconditions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: int = 1
    status: str = "active"
    test_type: str = "positive"

    def validate(self) -> None:
        """Validate the checklist definition."""

        string_fields = (
            "id",
            "title",
            "category",
            "description",
            "priority",
            "status",
            "test_type",
        )

        for name in string_fields:
            value = getattr(self, name)

            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{name} must be a non-empty string."
                )

        if self.priority not in {
            "critical", "high", "medium", "low"
        }:
            raise ValueError("Invalid checklist priority.")

        if self.status not in {
            "active", "draft", "deprecated"
        }:
            raise ValueError("Invalid checklist status.")

        if self.test_type not in {
            "positive", "negative", "boundary", "persistence"
        }:
            raise ValueError("Invalid checklist test_type.")

        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("version must be a positive integer.")

        for name in (
            "steps",
            "goal_conditions",
            "preconditions",
            "tags",
        ):
            value = getattr(self, name)

            if not isinstance(value, list):
                raise ValueError(f"{name} must be a list.")

            if any(
                not isinstance(item, str) or not item.strip()
                for item in value
            ):
                raise ValueError(
                    f"Every item in {name} must be a non-empty string."
                )

        if not self.steps:
            raise ValueError(
                "A checklist must contain at least one step."
            )

        if not self.goal_conditions:
            raise ValueError(
                "A checklist must contain at least one goal condition."
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checklist:
        """Build a validated checklist from dictionary data."""

        if not isinstance(data, dict):
            raise ValueError("Checklist data must be an object.")

        allowed = {
            "id",
            "title",
            "category",
            "description",
            "steps",
            "goal_conditions",
            "priority",
            "preconditions",
            "tags",
            "version",
            "status",
            "test_type",
        }

        unknown = set(data) - allowed

        if unknown:
            raise ValueError(
                f"Unknown checklist fields: {sorted(unknown)}"
            )

        try:
            checklist = cls(**data)
        except TypeError as exc:
            raise ValueError(
                f"Invalid checklist fields: {exc}"
            ) from exc

        checklist.validate()
        return checklist

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        self.validate()
        return asdict(self)

    def to_prompt(self) -> str:
        """Format requirements for checklist resolution."""

        self.validate()

        lines = [
            f"Checklist: {self.title}",
            f"ID: {self.id}",
            f"Category: {self.category}",
            f"Description: {self.description}",
            f"Test type: {self.test_type}",
        ]

        if self.preconditions:
            lines.append("\nPreconditions:")
            lines.extend(
                f"- {condition}"
                for condition in self.preconditions
            )

        lines.append("\nRequired user journey, in order:")
        lines.extend(
            f"{index}. {step}"
            for index, step in enumerate(self.steps, start=1)
        )

        lines.append("\nExpected goal conditions:")
        lines.extend(
            f"- {condition}"
            for condition in self.goal_conditions
        )

        lines.append(
            "\nImportant: Preconditions and goal conditions are not "
            "navigation checkpoints. Only map the required user journey."
        )

        return "\n".join(lines)