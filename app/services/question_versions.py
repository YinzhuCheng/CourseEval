"""Question version snapshots for grading history and teacher edits."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Question, QuestionVersion


def _serialize_question(question: Question) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": question.title,
        "description": question.description,
        "question_type": question.question_type.value,
        "max_score": str(question.max_score),
        "scoring_rule_override": question.scoring_rule_override.value if question.scoring_rule_override else None,
    }
    if question.code_config:
        cfg = question.code_config
        payload["code_config"] = {
            "input_spec": cfg.input_spec,
            "output_spec": cfg.output_spec,
            "visible_tests_json": cfg.visible_tests_json,
            "hidden_tests_json": cfg.hidden_tests_json,
            "allowed_libraries_note": cfg.allowed_libraries_note,
            "allowed_languages_json": cfg.allowed_languages_json,
            "reference_solution_python": cfg.reference_solution_python,
            "reference_solution_c": cfg.reference_solution_c,
            "reference_solution_cpp": cfg.reference_solution_cpp,
            "time_limit_seconds": cfg.time_limit_seconds,
            "memory_limit_mb": cfg.memory_limit_mb,
            "cpu_limit": cfg.cpu_limit,
            "allow_network": cfg.allow_network,
        }
    if question.short_answer_config:
        cfg = question.short_answer_config
        payload["short_answer_config"] = {
            "min_length": cfg.min_length,
            "max_length": cfg.max_length,
            "rubric_text": cfg.rubric_text,
            "llm_suggestion_enabled": cfg.llm_suggestion_enabled,
            "teacher_confirmation_required": cfg.teacher_confirmation_required,
        }
    if question.file_question_config:
        cfg = question.file_question_config
        payload["file_question_config"] = {
            "accepted_extensions": cfg.accepted_extensions,
            "reference_answer_text": cfg.reference_answer_text,
            "reference_answer_file_path": cfg.reference_answer_file_path,
            "rubric_text": cfg.rubric_text,
            "llm_suggestion_enabled": cfg.llm_suggestion_enabled,
            "teacher_confirmation_required": cfg.teacher_confirmation_required,
            "notebook_outputs_required": cfg.notebook_outputs_required,
        }
    return payload


def create_initial_question_version(db: Session, question: Question) -> QuestionVersion:
    row = QuestionVersion(
        question_id=question.id,
        version_number=1,
        snapshot_json=json.dumps(_serialize_question(question), ensure_ascii=True),
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    question.current_question_version_id = row.id
    return row


def append_question_version_after_edit(db: Session, question: Question) -> QuestionVersion:
    last_no = (
        db.query(QuestionVersion.version_number)
        .filter(QuestionVersion.question_id == question.id)
        .order_by(QuestionVersion.version_number.desc())
        .limit(1)
        .scalar()
    )
    next_no = int(last_no or 0) + 1
    row = QuestionVersion(
        question_id=question.id,
        version_number=next_no,
        snapshot_json=json.dumps(_serialize_question(question), ensure_ascii=True),
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    question.current_question_version_id = row.id
    return row

