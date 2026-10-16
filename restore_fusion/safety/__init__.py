"""Seguridad: confirmaciones, aprobaciones de borrado y plan de retención."""

from .approval import Approval, ApprovalError, DeleteReport, apply_deletions
from .confirm import ask_delete, confirm, confirm_action
from .retention_plan import PrunePlan, RetentionPolicy, approve_retention, plan_prune

__all__ = [
    "Approval",
    "ApprovalError",
    "DeleteReport",
    "apply_deletions",
    "ask_delete",
    "confirm",
    "confirm_action",
    "PrunePlan",
    "RetentionPolicy",
    "approve_retention",
    "plan_prune",
]
