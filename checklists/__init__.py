"""
Checklist package initialization and public interface.

This package provides the application-independent checklist management
system used by Blindspot to represent, store, retrieve, and validate
user-defined application testing requirements.

It exposes Checklist, the checklist specification model, and
ChecklistBank, the persistent checklist repository.

The package provides the specification and storage layer connecting
user-defined testing requirements with Blindspot's semantic resolution
and downstream testing pipelines.
"""

from checklists.bank import ChecklistBank
from checklists.models import Checklist

__all__ = ["ChecklistBank", "Checklist"]