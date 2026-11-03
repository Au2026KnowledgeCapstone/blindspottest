
"""
Provide persistent storage and management for Blindspot QA checklists.

This module implements ChecklistBank, which manages the loading,
validation, retrieval, filtering, addition, updating, and deletion of
checklist definitions.

Checklist definitions are stored in a versioned JSON library rather
than being embedded in application-specific testing logic.

The bank converts JSON definitions into validated Checklist objects
before making them available to the rest of Blindspot.

It provides:
- Loading and validation of checklist definitions.
- Retrieval by stable checklist identifier.
- Filtering by category, priority, and tags.
- Persistent addition, updating, and deletion.
- JSON serialization of the checklist collection.

Changes are written through a temporary file before replacing the
existing library file. The in-memory collection is updated only after
a successful write.

The bank does not interpret natural-language requirements, inspect
the AppGraph, or execute application tests.

Its responsibility is to provide a reliable source of structured
testing requirements for Blindspot's downstream components.

The JSON implementation is intended for local use and does not
currently provide multi-process write coordination or database
transactions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from checklists.models import Checklist


class ChecklistBank:
    """Persistent repository for checklist definitions."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | Path | None = None,
    ) -> None:
        self.path = (
            Path(path)
            if path is not None
            else Path(__file__).parent / "data" / "checklists.json"
        )

        self.checklists: dict[str, Checklist] = {}
        self.load()

    def load(self) -> None:
        """Load and validate checklist definitions."""

        if not self.path.exists():
            raise FileNotFoundError(
                f"Checklist file not found: {self.path}"
            )

        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid checklist JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError("Checklist file must contain an object.")

        if set(data) != {"version", "checklists"}:
            raise ValueError(
                "Checklist file must contain 'version' and 'checklists'."
            )

        version = data["version"]

        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != self.SCHEMA_VERSION
        ):
            raise ValueError("Unsupported checklist schema version.")

        entries = data["checklists"]

        if not isinstance(entries, list):
            raise ValueError("'checklists' must be a list.")

        loaded = {}

        for index, entry in enumerate(entries):
            try:
                checklist = Checklist.from_dict(entry)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Invalid checklist at index {index}: {exc}"
                ) from exc

            if checklist.id in loaded:
                raise ValueError(
                    f"Duplicate checklist ID: {checklist.id}"
                )

            loaded[checklist.id] = checklist

        self.checklists = loaded

    def _write(
        self,
        collection: dict[str, Checklist],
    ) -> None:
        """Write a candidate collection to persistent storage."""

        self.path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {
            "version": self.SCHEMA_VERSION,
            "checklists": [
                checklist.to_dict()
                for checklist in collection.values()
            ],
        }

        temporary_path = self.path.with_name(
            self.path.name + ".tmp"
        )

        try:
            with temporary_path.open("w", encoding="utf-8") as file:
                json.dump(
                    data,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )
                file.write("\n")

            temporary_path.replace(self.path)

        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _commit(
        self,
        candidate: dict[str, Checklist],
    ) -> None:
        """Persist changes before updating in-memory state."""

        self._write(candidate)
        self.checklists = candidate

    def save(self) -> None:
        """Persist the current checklist collection."""

        self._write(self.checklists)

    def get(self, checklist_id: str) -> Checklist:
        """Retrieve a checklist by ID."""

        try:
            return self.checklists[checklist_id]
        except KeyError:
            raise KeyError(
                f"Checklist not found: {checklist_id}"
            ) from None

    def list_all(self) -> list[Checklist]:
        """Return all registered checklists."""

        return list(self.checklists.values())

    def list_by_category(
        self,
        category: str,
    ) -> list[Checklist]:
        """Return checklists belonging to a category."""

        return [
            item
            for item in self.checklists.values()
            if item.category == category
        ]

    def list_by_priority(
        self,
        priority: str,
    ) -> list[Checklist]:
        """Return checklists matching a priority."""

        return [
            item
            for item in self.checklists.values()
            if item.priority == priority
        ]

    def list_by_tag(
        self,
        tag: str,
    ) -> list[Checklist]:
        """Return checklists containing a particular tag."""

        return [
            item
            for item in self.checklists.values()
            if tag in item.tags
        ]

    def add(self, checklist: Checklist) -> None:
        """Add a new checklist."""

        checklist.validate()

        if checklist.id in self.checklists:
            raise ValueError(
                f"Checklist already exists: {checklist.id}"
            )

        candidate = dict(self.checklists)
        candidate[checklist.id] = checklist

        self._commit(candidate)

    def update(self, checklist: Checklist) -> None:
        """Update an existing checklist."""

        checklist.validate()

        if checklist.id not in self.checklists:
            raise KeyError(
                f"Cannot update missing checklist: {checklist.id}"
            )

        candidate = dict(self.checklists)
        candidate[checklist.id] = checklist

        self._commit(candidate)

    def delete(self, checklist_id: str) -> None:
        """Delete a checklist."""

        if checklist_id not in self.checklists:
            raise KeyError(
                f"Checklist not found: {checklist_id}"
            )

        candidate = dict(self.checklists)
        del candidate[checklist_id]

        self._commit(candidate)