"""Question version snapshots for grading history and teacher edits."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.constants import QuestionType, ScoringRule
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
    if question.python_code_config:
        cfg = question.python_code_config
        payload["python_code_config"] = {
            "input_spec": cfg.input_spec,
            "output_spec": cfg.output_spec,
            "visible_tests_json": cfg.visible_tests_json,
            "hidden_tests_json": cfg.hidden_tests_json,
            "allowed_libraries_note": cfg.allowed_libraries_note,
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


def restore_question_from_version_payload(db: Session, question: Question, payload: dict[str, Any]) -> None:
    question.title = str(payload.get("title") or question.title)
    question.description = payload.get("description")
    if payload.get("question_type"):
        question.question_type = QuestionType(payload["question_type"])
    if payload.get("max_score") is not None:
        question.max_score = Decimal(str(payload["max_score"]))
    override = payload.get("scoring_rule_override")
    question.scoring_rule_override = ScoringRule(override) if override else None

    py = payload.get("python_code_config")
    if py and question.python_code_config:
        cfg = question.python_code_config
        cfg.input_spec = py.get("input_spec")
        cfg.output_spec = py.get("output_spec")
        cfg.visible_tests_json = py.get("visible_tests_json") or "[]"
        cfg.hidden_tests_json = py.get("hidden_tests_json") or "[]"
        cfg.allowed_libraries_note = py.get("allowed_libraries_note")
        if py.get("time_limit_seconds") is not None:
            cfg.time_limit_seconds = int(py["time_limit_seconds"])
        if py.get("memory_limit_mb") is not None:
            cfg.memory_limit_mb = int(py["memory_limit_mb"])
        if py.get("cpu_limit") is not None:
            cfg.cpu_limit = str(py["cpu_limit"])
        if py.get("allow_network") is not None:
            cfg.allow_network = bool(py["allow_network"])

    sa = payload.get("short_answer_config")
    if sa and question.short_answer_config:
        cfg = question.short_answer_config
        cfg.min_length = sa.get("min_length")
        cfg.max_length = sa.get("max_length")
        cfg.rubric_text = sa.get("rubric_text")
        if sa.get("llm_suggestion_enabled") is not None:
            cfg.llm_suggestion_enabled = bool(sa["llm_suggestion_enabled"])
        if sa.get("teacher_confirmation_required") is not None:
            cfg.teacher_confirmation_required = bool(sa["teacher_confirmation_required"])

    fq = payload.get("file_question_config")
    if fq and question.file_question_config:
        cfg = question.file_question_config
        if fq.get("accepted_extensions"):
            cfg.accepted_extensions = str(fq["accepted_extensions"])
        if fq.get("reference_answer_text") is not None:
            cfg.reference_answer_text = str(fq["reference_answer_text"])
        if fq.get("rubric_text") is not None:
            cfg.rubric_text = str(fq["rubric_text"])
        if fq.get("llm_suggestion_enabled") is not None:
            cfg.llm_suggestion_enabled = bool(fq["llm_suggestion_enabled"])
        if fq.get("teacher_confirmation_required") is not None:
            cfg.teacher_confirmation_required = bool(fq["teacher_confirmation_required"])
        if fq.get("notebook_outputs_required") is not None:
            cfg.notebook_outputs_required = bool(fq["notebook_outputs_required"])


def ensure_question_has_current_version(db: Session, question: Question) -> None:
    if question.current_question_version_id is not None:
        return
    create_initial_question_version(db, question)
