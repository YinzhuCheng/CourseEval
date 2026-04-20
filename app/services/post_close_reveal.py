"""After assignment close_at: reference answer, rubric, and optional anonymous full-score sample."""

from __future__ import annotations

import random
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import QuestionType
from app.models import FinalGradeSnapshot, Question, Submission
from app.services.assignment_visibility import assignment_reference_bundle_public
from app.services.storage_paths import absolute_data_path
from app.services.submissions import read_submission_text_artifact


def reveal_bundle_for_question(db: Session, question: Question) -> dict:
    """Public-safe content for students after the reference bundle deadline (close_at, else due_at)."""
    asn = question.assignment
    if not assignment_reference_bundle_public(asn):
        return {"open": False, "reference_text": None, "rubric_text": None, "sample": None}

    reference_text: str | None = None
    rubric_text: str | None = None

    qt = question.question_type
    if qt == QuestionType.SHORT_ANSWER and question.short_answer_config:
        sac = question.short_answer_config
        reference_text = None
        rubric_text = (sac.rubric_text or "").strip() or None
    elif qt == QuestionType.CODE and question.code_config:
        parts = []
        c = question.code_config
        if c.reference_solution_python.strip():
            parts.append("### Python\n```\n" + c.reference_solution_python.strip() + "\n```")
        if c.reference_solution_c.strip():
            parts.append("### C\n```\n" + c.reference_solution_c.strip() + "\n```")
        if c.reference_solution_cpp.strip():
            parts.append("### C++\n```\n" + c.reference_solution_cpp.strip() + "\n```")
        reference_text = "\n\n".join(parts) if parts else None
        rubric_text = "Visible/hidden tests per question configuration."
    elif question.file_question_config:
        fqc = question.file_question_config
        reference_text = (fqc.reference_answer_text or "").strip() or None
        rubric_text = (fqc.rubric_text or "").strip() or None

    sample = _pick_anonymous_full_score_sample(db, question)
    return {
        "open": True,
        "reference_text": reference_text,
        "rubric_text": rubric_text,
        "sample": sample,
    }


def _pick_anonymous_full_score_sample(db: Session, question: Question) -> dict | None:
    max_score = question.max_score
    if max_score is None:
        return None
    max_dec = max_score if isinstance(max_score, Decimal) else Decimal(str(max_score))

    rows = db.execute(
        select(FinalGradeSnapshot.student_id, FinalGradeSnapshot.effective_submission_id, FinalGradeSnapshot.score).where(
            FinalGradeSnapshot.question_id == question.id,
            FinalGradeSnapshot.score.isnot(None),
        )
    ).all()
    candidates: list[tuple[int, int]] = []
    for student_id, eff_sub_id, score in rows:
        if score is None:
            continue
        sdec = score if isinstance(score, Decimal) else Decimal(str(score))
        if sdec >= max_dec and eff_sub_id:
            candidates.append((int(student_id), int(eff_sub_id)))
    if not candidates:
        return None
    _student_id, sub_id = random.choice(candidates)
    submission = db.get(Submission, sub_id)
    if submission is None:
        return None

    preview = _submission_preview_text(submission)
    return {
        "submission_id": submission.id,
        "preview_text": preview,
        "question_type": submission.submission_type.value,
    }


def _submission_preview_text(submission: Submission) -> str:
    qt = submission.submission_type
    if qt == QuestionType.CODE and submission.stored_file_path:
        try:
            p = absolute_data_path(submission.stored_file_path)
            if p.exists():
                return p.read_text(encoding="utf-8", errors="replace")[:12000]
        except (OSError, ValueError):
            return ""
    if submission.answer_text:
        return (submission.answer_text or "")[:12000]
    return read_submission_text_artifact(submission, "summary", max_chars=8000)
