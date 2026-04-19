import unittest
from datetime import timedelta

from app.db import utcnow
from app.models import Assignment
from app.services.assignment_visibility import assignment_reference_bundle_public, assignment_rubric_public


class AssignmentVisibilityTests(unittest.TestCase):
    def test_rubric_uses_due_before_close(self) -> None:
        past = utcnow() - timedelta(hours=1)
        future = utcnow() + timedelta(hours=2)
        asn = Assignment(
            course_id=1,
            title="t",
            due_at=past,
            close_at=future,
        )
        self.assertTrue(assignment_rubric_public(asn))

    def test_rubric_hidden_before_due(self) -> None:
        future = utcnow() + timedelta(hours=1)
        asn = Assignment(course_id=1, title="t", due_at=future, close_at=None)
        self.assertFalse(assignment_rubric_public(asn))

    def test_rubric_falls_back_to_close_when_no_due(self) -> None:
        past = utcnow() - timedelta(hours=1)
        asn = Assignment(course_id=1, title="t", due_at=None, close_at=past)
        self.assertTrue(assignment_rubric_public(asn))

    def test_reference_bundle_uses_close_when_set(self) -> None:
        due_past = utcnow() - timedelta(hours=2)
        close_future = utcnow() + timedelta(hours=1)
        asn = Assignment(course_id=1, title="t", due_at=due_past, close_at=close_future)
        self.assertTrue(assignment_rubric_public(asn))
        self.assertFalse(assignment_reference_bundle_public(asn))


if __name__ == "__main__":
    unittest.main()
