"""When students may see rubrics, reference answers, etc."""

from __future__ import annotations

from datetime import datetime, timezone

from app.db import utcnow
from app.models import Assignment


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def assignment_rubric_public(assignment: Assignment) -> bool:
    """True when the assignment deadline for showing rubric to students has passed.

    Uses ``due_at`` when set (primary \"截止\"); otherwise ``close_at``; if neither is set, rubric is visible.
    """
    due = _as_utc(assignment.due_at)
    if due is not None:
        return utcnow() >= due
    close_at = _as_utc(assignment.close_at)
    if close_at is not None:
        return utcnow() >= close_at
    return True


def assignment_reference_bundle_public(assignment: Assignment) -> bool:
    """Post-close reference answer + sample bundle: use close_at when set, else due_at, else open."""
    close_at = _as_utc(assignment.close_at)
    if close_at is not None:
        return utcnow() >= close_at
    due = _as_utc(assignment.due_at)
    if due is not None:
        return utcnow() >= due
    return True
