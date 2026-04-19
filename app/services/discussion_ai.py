"""@AI / discussion AI replies: model selection, prompts, billing to requesting user."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.constants import DiscussionTopicKind
from app.models import Course, CourseMaterial, DiscussionPost, DiscussionTopic, LLMConfig, PlatformLlmTokenPolicy, Question, User
from app.services.discussions import create_post
from app.services.llm import generate_text
from app.services.llm_groups import (
    available_llm_groups_for_course,
    first_group_with_any_enabled_target,
    first_valid_group,
    group_has_any_enabled_target,
    latest_platform_llm_group,
    latest_platform_llm_group_relaxed,
    ordered_group_targets,
    parse_optional_tested_llm_group_id,
)

if TYPE_CHECKING:
    pass

_AI_MENTION_RE = re.compile(r"(?i)@AI\b")


def strip_ai_mentions(body: str) -> str:
    cleaned = _AI_MENTION_RE.sub("", body or "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def message_requests_discussion_ai(body: str, request_ai_flag: bool) -> bool:
    if request_ai_flag:
        return True
    return bool(_AI_MENTION_RE.search(body or ""))


def _policy_row(db: Session) -> PlatformLlmTokenPolicy:
    row = db.get(PlatformLlmTokenPolicy, 1)
    if row is None:
        row = PlatformLlmTokenPolicy(id=1, default_user_daily_llm_tokens=100000)
        db.add(row)
        db.flush()
    return row


def parse_optional_tested_llm_config_id(db: Session, raw: str) -> int | None:
    return parse_optional_tested_llm_group_id(db, raw)


def _first_discussion_config(db: Session, config_id: int | None) -> LLMConfig | None:
    """Prefer connectivity-tested groups; accept enabled-but-untested like grading fallbacks."""
    g = first_valid_group(db, config_id)
    if g:
        return g
    return first_group_with_any_enabled_target(db, config_id)


def resolve_discussion_ai_llm_config(db: Session, topic: DiscussionTopic) -> LLMConfig | None:
    """Precedence: course override → platform kind override → platform default override → latest platform group."""
    course = db.get(Course, topic.course_id)
    policy = _policy_row(db)

    if topic.kind == DiscussionTopicKind.QUESTION:
        if course and course.discussion_ai_question_llm_config_id:
            c = _first_discussion_config(db, course.discussion_ai_question_llm_config_id)
            if c:
                return c
        c = _first_discussion_config(db, policy.discussion_ai_question_llm_config_id)
        if c:
            return c
    elif topic.kind == DiscussionTopicKind.COURSE_MATERIAL:
        if course and course.discussion_ai_material_llm_config_id:
            c = _first_discussion_config(db, course.discussion_ai_material_llm_config_id)
            if c:
                return c
        c = _first_discussion_config(db, policy.discussion_ai_material_llm_config_id)
        if c:
            return c

    c = _first_discussion_config(db, policy.discussion_ai_default_llm_config_id)
    if c:
        return c
    return latest_platform_llm_group(db) or latest_platform_llm_group_relaxed(db)


def resolve_selected_discussion_ai_llm_config(
    db: Session,
    topic: DiscussionTopic,
    selected_group_id: int | None,
) -> LLMConfig | None:
    if selected_group_id:
        group = first_valid_group(db, selected_group_id)
        if group is None:
            group = first_group_with_any_enabled_target(db, selected_group_id)
        if group and (group.course_id in (None, topic.course_id)):
            return group
    return resolve_discussion_ai_llm_config(db, topic)


def _grading_like_llm_fallback(db: Session, topic: DiscussionTopic) -> LLMConfig | None:
    """Match auto-grading fallback: course default (when not using global) → latest platform group."""
    course = db.get(Course, topic.course_id)
    if course is not None and not course.use_global_llm_default:
        g = course.default_llm_config
        if g is not None and group_has_any_enabled_target(g):
            return g
    return latest_platform_llm_group(db) or latest_platform_llm_group_relaxed(db)


def discussion_ai_group_options(db: Session, topic: DiscussionTopic) -> list[dict[str, object]]:
    return available_llm_groups_for_course(db, topic.course_id)


def get_or_create_ai_assistant_user(db: Session) -> User:
    username = "__course_ai_assistant__"
    u = db.scalar(select(User).where(User.username == username))
    if u:
        return u
    from app.constants import AccountRole, PlatformRole

    u = User(
        username=username,
        email="ai-assistant@system.internal",
        password_hash="!",
        account_role=AccountRole.STUDENT,
        platform_role=PlatformRole.USER,
        email_verified=True,
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _question_context_text(db: Session, question_id: int) -> str:
    q = db.scalar(
        select(Question)
        .options(joinload(Question.assignment))
        .where(Question.id == question_id)
    )
    if q is None:
        return ""
    parts = [
        f"Question title: {q.title}",
        f"Question description:\n{q.description or ''}",
        f"Question type: {q.question_type.value}",
        f"Max score: {q.max_score}",
    ]
    return "\n".join(parts)


def _material_context_text(db: Session, material_id: int) -> str:
    m = db.get(CourseMaterial, material_id)
    if m is None:
        return ""
    body = (m.body_markdown or "").strip()
    if len(body) > 12000:
        body = body[:12000] + "\n...[truncated]"
    parts = [f"Material title: {m.title}", f"External link: {m.external_url or 'none'}", f"Body (markdown/plain):\n{body}"]
    return "\n".join(parts)


def build_discussion_ai_prompts(db: Session, topic: DiscussionTopic, user_message: str) -> tuple[str, str]:
    ctx = ""
    if topic.kind == DiscussionTopicKind.QUESTION and topic.question_id:
        ctx = _question_context_text(db, topic.question_id)
    elif topic.kind == DiscussionTopicKind.COURSE_MATERIAL and topic.course_material_id:
        ctx = _material_context_text(db, topic.course_material_id)

    system = (
        "You are a helpful teaching assistant in a course discussion. "
        "Answer clearly and concisely. If the question is outside the provided course context, say so briefly. "
        "Do not fabricate private information about students. "
        "Do not include markdown images, raw URLs, or links in your reply (plain text and simple markdown only: "
        "bold, italic, lists, code fences)."
    )
    user_block = f"Course context (for this thread):\n{ctx}\n\nStudent message:\n{user_message}"
    return system, user_block


def run_discussion_ai_reply(
    db: Session,
    *,
    topic: DiscussionTopic,
    requester: User,
    user_message: str,
    parent_post_id: int | None,
    selected_group_id: int | None = None,
) -> DiscussionPost | None:
    """Create AI reply post; charges requester's daily LLM quota. Returns None if no config or empty message."""
    user_message = (user_message or "").strip()
    if not user_message:
        return None
    group = resolve_selected_discussion_ai_llm_config(db, topic, selected_group_id)
    if group is None or not group_has_any_enabled_target(group):
        group = _grading_like_llm_fallback(db, topic)
    if group is None or not group_has_any_enabled_target(group):
        raise ValueError(
            "No discussion AI group is available. An administrator must configure an enabled LLM group "
            "under Admin → LLM (Discussion AI defaults) or course-level overrides."
        )

    system_prompt, prompt = build_discussion_ai_prompts(db, topic, user_message)
    result = None
    last_exc: Exception | None = None
    for tested_only in (True, False):
        for target in ordered_group_targets(group, tested_only=tested_only):
            try:
                result = generate_text(
                    target,
                    prompt,
                    system_prompt,
                    bill_user_id=requester.id,
                    bill_db=db,
                    bill_user_prompt=prompt,
                    bill_system_prompt=system_prompt,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if result is not None:
            break
    if result is None:
        raise ValueError(f"All LLMs in group '{group.name}' failed.") from last_exc
    text = (result.content or "").strip()
    if not text:
        text = "(AI produced an empty response.)"

    ai_user = get_or_create_ai_assistant_user(db)
    body = f"[AI]\n{text}"
    post = create_post(
        db,
        topic_id=topic.id,
        author=ai_user,
        body=body,
        parent_post_id=parent_post_id,
        is_anonymous=False,
        is_ai=True,
    )
    return post


def create_user_post_and_maybe_ai_reply(
    db: Session,
    *,
    topic: DiscussionTopic,
    user: User,
    body: str,
    parent_post_id: int | None,
    is_anonymous: bool,
    request_ai: bool,
    pending_image_uploads: bool = False,
    selected_llm_group_id: int | None = None,
) -> tuple[DiscussionPost | None, str | None]:
    """Create user post when there is body; AI-only button with parent skips user post. Returns (user_post_or_none, ai_error_message)."""
    raw = (body or "").strip()
    wants_ai = message_requests_discussion_ai(raw, request_ai)
    user_body = strip_ai_mentions(raw) if wants_ai else raw

    ai_err: str | None = None
    if wants_ai and not user_body and not pending_image_uploads and parent_post_id:
        parent = db.get(DiscussionPost, parent_post_id)
        if parent is None or parent.topic_id != topic.id:
            raise ValueError("empty_body")
        prompt = (parent.body_text or "").strip() or "Please respond helpfully to this message."
        try:
            run_discussion_ai_reply(
                db,
                topic=topic,
                requester=user,
                user_message=prompt,
                parent_post_id=parent_post_id,
                selected_group_id=selected_llm_group_id,
            )
        except ValueError as e:
            ai_err = str(e)
        except Exception as e:
            ai_err = str(e) or "AI request failed."
        return None, ai_err

    if not user_body and not pending_image_uploads:
        raise ValueError("empty_body")

    user_post = create_post(
        db,
        topic_id=topic.id,
        author=user,
        body=user_body,
        parent_post_id=parent_post_id,
        is_anonymous=is_anonymous,
        is_ai=False,
        has_pending_image_uploads=pending_image_uploads and not user_body,
    )
    if wants_ai:
        prompt = strip_ai_mentions(raw)
        try:
            run_discussion_ai_reply(
                db,
                topic=topic,
                requester=user,
                user_message=prompt,
                parent_post_id=user_post.id,
                selected_group_id=selected_llm_group_id,
            )
        except ValueError as e:
            ai_err = str(e)
        except Exception as e:
            ai_err = str(e) or "AI request failed."
    return user_post, ai_err
