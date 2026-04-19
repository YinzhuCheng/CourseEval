"""Unit tests for discussion @AI mention handling."""

from app.services.discussion_ai import (
    message_requests_discussion_ai,
    parse_ai_context_mode,
    strip_ai_mentions,
)


def test_message_requests_ai_anywhere_in_body() -> None:
    assert message_requests_discussion_ai("Hello @AI please explain", False) is True
    assert message_requests_discussion_ai("  @AI\nhelp", False) is True


def test_strip_ai_mentions_removes_all() -> None:
    assert strip_ai_mentions("Hello @AI please explain") == "Hello please explain"
    assert strip_ai_mentions("@AI only") == "only"


def test_parse_ai_context_mode_clamps_k() -> None:
    assert parse_ai_context_mode("recent_k", "5", topic_post_count=3) == ("recent_k", 3)
    assert parse_ai_context_mode("full", "99", topic_post_count=10) == ("full", 10)
    assert parse_ai_context_mode(None, None, topic_post_count=1) == ("recent_k", 1)
