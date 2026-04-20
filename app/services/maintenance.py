"""Best-effort repairs for data produced by older application versions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.constants import EvaluationTaskStatus, SubmissionStatus
from app.db import utcnow
from app.models import CourseMaterial, EvaluationTask, FinalGradeSnapshot, FreeDiscussionTopic, Submission
from app.services.courses import OPEN_COMMUNITY_COURSE_CODE
from app.services.submissions import cleanup_stale_running_items, update_final_grade_snapshot


def _backfill_free_discussion_chapters(db: Session) -> int:
    rows = list(
        db.scalars(
            select(CourseMaterial)
            .join(CourseMaterial.course)
            .where(
                CourseMaterial.free_discussion_topic_id.is_(None),
                CourseMaterial.sort_order >= 100000,
                CourseMaterial.course.has(code=OPEN_COMMUNITY_COURSE_CODE),
            )
        ).all()
    )
    changed = 0
    for material in rows:
        topic_id = (material.sort_order or 0) // 100000
        if topic_id <= 0:
            continue
        topic = db.get(FreeDiscussionTopic, topic_id)
        if topic is None or topic.course_id != material.course_id:
            continue
        material.free_discussion_topic_id = topic.id
        changed += 1
    return changed


def _repair_task_statuses(db: Session) -> int:
    changed = cleanup_stale_running_items()
    finished_at = utcnow()
    orphan_tasks = list(
        db.scalars(
            select(EvaluationTask)
            .options(joinedload(EvaluationTask.submission))
            .where(
                EvaluationTask.status == EvaluationTaskStatus.QUEUED,
                EvaluationTask.backend_job_id.is_(None),
            )
        ).unique()
    )
    for task in orphan_tasks:
        task.status = EvaluationTaskStatus.FAILED
        task.finished_at = finished_at
        task.error_message = task.error_message or "Evaluation task was created but never enqueued."
        if task.submission and task.submission.status in {SubmissionStatus.SUBMITTED, SubmissionStatus.QUEUED}:
            task.submission.status = SubmissionStatus.FAILED_SYSTEM
            task.submission.completed_at = finished_at
            task.submission.counts_toward_limit = False
            task.submission.is_effective_submission = False
            task.submission.failure_reason_code = "system_error"
        changed += 1
    return changed


def _repair_grade_snapshots(db: Session) -> int:
    pairs = set(db.execute(select(Submission.question_id, Submission.user_id)).all())
    snapshot_pairs = set(db.execute(select(FinalGradeSnapshot.question_id, FinalGradeSnapshot.student_id)).all())
    changed = 0
    for question_id, user_id in pairs | snapshot_pairs:
        submission = db.scalar(
            select(Submission)
            .options(
                joinedload(Submission.question).joinedload(Submission.assignment),
                joinedload(Submission.evaluation_results),
                joinedload(Submission.feedback_items),
                joinedload(Submission.question_version),
            )
            .where(Submission.question_id == question_id, Submission.user_id == user_id)
            .order_by(Submission.submitted_at.desc())
        )
        if submission is None:
            snapshot = db.scalar(
                select(FinalGradeSnapshot).where(
                    FinalGradeSnapshot.question_id == question_id,
                    FinalGradeSnapshot.student_id == user_id,
                )
            )
            if snapshot is not None:
                snapshot.effective_submission_id = None
                snapshot.score = None
                snapshot.feedback_source = None
                snapshot.question_version_id = None
                snapshot.updated_at = utcnow()
                changed += 1
            continue
        update_final_grade_snapshot(db, submission)
        changed += 1
    return changed


def repair_existing_state(db: Session) -> dict[str, int]:
    return {
        "free_discussion_chapters": _backfill_free_discussion_chapters(db),
        "tasks": _repair_task_statuses(db),
        "grade_snapshots": _repair_grade_snapshots(db),
    }
