"""Effective score and teacher-review rules for submissions."""

from __future__ import annotations

import json
from decimal import Decimal

from app.constants import FeedbackSource, QuestionType
from app.models import Feedback, Submission


def latest_feedback(submission: Submission, source: FeedbackSource) -> Feedback | None:
    matching_feedback = [item for item in submission.feedback_items if item.source == source]
    if not matching_feedback:
        return None
    return max(matching_feedback, key=lambda item: item.created_at)


def submission_requires_teacher_confirmation(submission: Submission) -> bool:
    if submission.question_version is not None and submission.question_version.snapshot_json:
        try:
            payload = json.loads(submission.question_version.snapshot_json)
        except json.JSONDecodeError:
            payload = {}
        if submission.submission_type == QuestionType.SHORT_ANSWER:
            cfg = payload.get("short_answer_config") or {}
            return bool(cfg.get("teacher_confirmation_required"))
        if submission.submission_type == QuestionType.FILE_LLM:
            cfg = payload.get("file_question_config") or {}
            return bool(cfg.get("teacher_confirmation_required"))

    question = submission.question
    if submission.submission_type == QuestionType.SHORT_ANSWER:
        config = question.short_answer_config if question is not None else None
        return bool(config and config.teacher_confirmation_required)
    if submission.submission_type == QuestionType.FILE_LLM:
        config = question.file_question_config if question is not None else None
        return bool(config and config.teacher_confirmation_required)
    return False


def submission_has_llm_score(submission: Submission) -> bool:
    return any(
        item.source == FeedbackSource.LLM and item.score_suggestion is not None
        for item in submission.feedback_items
    )


def submission_eligible_for_gradebook(submission: Submission) -> bool:
    if submission_requires_teacher_confirmation(submission) and not submission_has_teacher_feedback(submission):
        return False
    if submission.counts_toward_limit or submission.is_effective_submission:
        return True
    return False


def submission_has_teacher_feedback(submission: Submission) -> bool:
    return latest_feedback(submission, FeedbackSource.TEACHER) is not None


def is_submission_pending_teacher_review(submission: Submission) -> bool:
    if not submission_requires_teacher_confirmation(submission):
        return False
    return not submission_has_teacher_feedback(submission)


def resolve_submission_score(submission: Submission) -> tuple[Decimal | None, FeedbackSource | None]:
    teacher_feedback = latest_feedback(submission, FeedbackSource.TEACHER)
    if teacher_feedback is not None:
        return (
            Decimal(str(teacher_feedback.score_suggestion)) if teacher_feedback.score_suggestion is not None else None,
            FeedbackSource.TEACHER,
        )

    latest_result = submission.evaluation_results[-1] if submission.evaluation_results else None
    if latest_result is not None and latest_result.final_score is not None:
        return Decimal(str(latest_result.final_score)), FeedbackSource.AUTO

    llm_feedback = latest_feedback(submission, FeedbackSource.LLM)
    if llm_feedback is not None and llm_feedback.score_suggestion is not None:
        if submission_requires_teacher_confirmation(submission):
            return None, None
        return Decimal(str(llm_feedback.score_suggestion)), FeedbackSource.LLM

    auto_feedback = latest_feedback(submission, FeedbackSource.AUTO)
    if auto_feedback is not None and auto_feedback.score_suggestion is not None:
        return Decimal(str(auto_feedback.score_suggestion)), FeedbackSource.AUTO

    return None, None
