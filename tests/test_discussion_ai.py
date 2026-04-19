"""Unit tests for discussion @AI mention handling."""

from app.services.discussion_ai import message_requests_discussion_ai, strip_ai_mentions


def test_message_requests_ai_anywhere_in_body() -> None:
    assert message_requests_discussion_ai("Hello @AI please explain", False) is True
    assert message_requests_discussion_ai("  @AI\nhelp", False) is True


def test_strip_ai_mentions_removes_all() -> None:
    assert strip_ai_mentions("Hello @AI please explain") == "Hello please explain"
    assert strip_ai_mentions("@AI only") == "only"
