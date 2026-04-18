"""Aggregated statistics for teacher-facing dashboards."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.constants import CourseRole, MembershipStatus, SubmissionStatus
from app.models import Assignment, CourseMember, FinalGradeSnapshot, Question, Submission, User
from app.services.submissions import is_submission_pending_teacher_review


def active_student_ids(db: Session, course_id: int) -> list[int]:
    rows = db.execute(
        select(User.id)
        .join(CourseMember, CourseMember.user_id == User.id)
        .where(
            CourseMember.course_id == course_id,
            CourseMember.role == CourseRole.STUDENT,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
        .order_by(User.id.asc())
    ).all()
    return [int(r[0]) for r in rows]


def question_counts_by_assignment(db: Session, course_id: int) -> dict[int, int]:
    rows = db.execute(
        select(Assignment.id, func.count(Question.id))
        .outerjoin(Question, Question.assignment_id == Assignment.id)
        .where(Assignment.course_id == course_id)
        .group_by(Assignment.id)
    ).all()
    return {int(r[0]): int(r[1] or 0) for r in rows}


def distinct_questions_submitted_by_pair(db: Session, course_id: int, student_ids: list[int]) -> dict[tuple[int, int], int]:
    if not student_ids:
        return {}
    rows = db.execute(
        select(
            Submission.user_id,
            Submission.assignment_id,
            func.count(func.distinct(Submission.question_id)),
        )
        .where(
            Submission.course_id == course_id,
            Submission.user_id.in_(student_ids),
        )
        .group_by(Submission.user_id, Submission.assignment_id)
    ).all()
    return {(int(r[0]), int(r[1])): int(r[2] or 0) for r in rows}


def operations_summary_by_assignment(db: Session, course_id: int) -> dict[int, dict]:
    """One pass over course submissions: operational counts per assignment_id."""
    subs = (
        db.query(Submission)
        .options(
            joinedload(Submission.feedback_items),
            joinedload(Submission.question).joinedload(Question.short_answer_config),
            joinedload(Submission.question).joinedload(Question.file_question_config),
        )
        .filter(Submission.course_id == course_id)
        .all()
    )
    by_aid: dict[int, list[Submission]] = defaultdict(list)
    for s in subs:
        by_aid[int(s.assignment_id)].append(s)
    out: dict[int, dict] = {}
    for aid, lst in by_aid.items():
        pending = sum(1 for s in lst if is_submission_pending_teacher_review(s))
        stuck = sum(1 for s in lst if s.status in (SubmissionStatus.QUEUED, SubmissionStatus.RUNNING))
        failed = sum(1 for s in lst if s.status in (SubmissionStatus.FAILED_SYSTEM, SubmissionStatus.FAILED_ANSWER))
        late = sum(1 for s in lst if s.is_late)
        out[aid] = {
            "pending_teacher_review_count": pending,
            "stuck_evaluation_count": stuck,
            "failed_evaluation_count": failed,
            "late_submission_row_count": late,
        }
    return out


def compute_course_staff_overview(db: Session, course_id: int) -> dict:
    """Cross-assignment operational stats for the course detail KPI strip."""
    student_n = len(active_student_ids(db, course_id))
    by_asn = operations_summary_by_assignment(db, course_id)
    pending = sum(v["pending_teacher_review_count"] for v in by_asn.values())
    stuck = sum(v["stuck_evaluation_count"] for v in by_asn.values())
    failed = sum(v["failed_evaluation_count"] for v in by_asn.values())
    late_rows = sum(v["late_submission_row_count"] for v in by_asn.values())
    return {
        "active_student_count": student_n,
        "pending_teacher_review_count": pending,
        "stuck_evaluation_count": stuck,
        "failed_evaluation_count": failed,
        "late_submission_row_count": late_rows,
        "by_assignment": by_asn,
    }


def _score_stats(values: list[Decimal]) -> dict:
    nums = sorted(float(v) for v in values)
    n = len(nums)
    if n == 0:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "p25": None,
            "p75": None,
            "zero_count": 0,
        }
    zero_count = sum(1 for x in nums if x == 0.0)

    def _pctile(p: float) -> float:
        if n == 1:
            return nums[0]
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        if lo == hi:
            return nums[lo]
        return nums[lo] + (nums[hi] - nums[lo]) * (idx - lo)

    return {
        "count": n,
        "mean": sum(nums) / n,
        "median": float(median(nums)) if n else None,
        "min": nums[0],
        "max": nums[-1],
        "p25": _pctile(0.25),
        "p75": _pctile(0.75),
        "zero_count": zero_count,
    }


def compute_assignment_staff_stats(
    db: Session,
    assignment_id: int,
    course_id: int,
    student_ids: list[int],
) -> dict:
    """Per-assignment KPIs and per-question submission / grade summaries."""
    pending = 0
    stuck = 0
    failed = 0
    late_rows = 0
    subs = (
        db.query(Submission)
        .options(
            joinedload(Submission.feedback_items),
            joinedload(Submission.question).joinedload(Question.short_answer_config),
            joinedload(Submission.question).joinedload(Question.file_question_config),
        )
        .filter(Submission.assignment_id == assignment_id)
        .all()
    )
    for s in subs:
        if is_submission_pending_teacher_review(s):
            pending += 1
        if s.status in (SubmissionStatus.QUEUED, SubmissionStatus.RUNNING):
            stuck += 1
        if s.status in (SubmissionStatus.FAILED_SYSTEM, SubmissionStatus.FAILED_ANSWER):
            failed += 1
        if s.is_late:
            late_rows += 1

    question_rows = (
        db.execute(select(Question.id, Question.title, Question.order_index, Question.max_score).where(Question.assignment_id == assignment_id))
        .all()
    )
    questions_meta = [{"id": int(r[0]), "title": r[1], "order_index": int(r[2]), "max_score": r[3]} for r in question_rows]
    q_ids = [q["id"] for q in questions_meta]
    n_students = len(student_ids)

    submitted_by_q: dict[int, set[int]] = defaultdict(set)
    if student_ids and q_ids:
        sub_rows = db.execute(
            select(Submission.question_id, Submission.user_id)
            .where(
                Submission.assignment_id == assignment_id,
                Submission.user_id.in_(student_ids),
                Submission.question_id.in_(q_ids),
            )
            .distinct()
        ).all()
        for qid, uid in sub_rows:
            submitted_by_q[int(qid)].add(int(uid))

    scores_by_q: dict[int, list[Decimal]] = defaultdict(list)
    source_counts_by_q: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if student_ids and q_ids:
        snap_rows = db.execute(
            select(
                FinalGradeSnapshot.question_id,
                FinalGradeSnapshot.score,
                FinalGradeSnapshot.feedback_source,
            ).where(
                FinalGradeSnapshot.assignment_id == assignment_id,
                FinalGradeSnapshot.student_id.in_(student_ids),
                FinalGradeSnapshot.question_id.in_(q_ids),
            )
        ).all()
        for qid, score, fsrc in snap_rows:
            qid_i = int(qid)
            if score is not None:
                scores_by_q[qid_i].append(score if isinstance(score, Decimal) else Decimal(str(score)))
            if fsrc is not None:
                raw = fsrc.value if hasattr(fsrc, "value") else str(fsrc)
                source_counts_by_q[qid_i][raw] += 1

    question_stats = []
    for q in sorted(questions_meta, key=lambda x: (x["order_index"], x["id"])):
        qid = q["id"]
        sub_n = len(submitted_by_q.get(qid, ()))
        stats = _score_stats(scores_by_q.get(qid, []))
        missing = []
        if student_ids:
            have = submitted_by_q.get(qid, set())
            for sid in student_ids:
                if sid not in have:
                    u = db.get(User, sid)
                    missing.append({"id": sid, "username": u.username if u else str(sid)})
        question_stats.append(
            {
                "question": q,
                "submitted_student_count": sub_n,
                "missing_student_count": max(0, n_students - sub_n),
                "missing_students": missing[:200],
                "missing_truncated": max(0, n_students - sub_n - 200),
                "score_stats": stats,
                "feedback_source_counts": dict(source_counts_by_q.get(qid, {})),
            }
        )

    # Assignment-level: students with at least one submission vs all questions done
    students_with_any = set()
    students_all_done = set()
    if student_ids and q_ids:
        dist = distinct_questions_submitted_by_pair(db, course_id, student_ids)
        q_totals = question_counts_by_assignment(db, course_id)
        q_total = int(q_totals.get(assignment_id, 0))
        for sid in student_ids:
            k = (sid, assignment_id)
            nq = dist.get(k, 0)
            if nq > 0:
                students_with_any.add(sid)
            if q_total > 0 and nq >= q_total:
                students_all_done.add(sid)
    elif student_ids:
        students_with_any = set()
        students_all_done = set(student_ids) if not q_ids else set()

    by_qid = {item["question"]["id"]: item for item in question_stats}
    return {
        "active_student_count": n_students,
        "pending_teacher_review_count": pending,
        "stuck_evaluation_count": stuck,
        "failed_evaluation_count": failed,
        "late_submission_row_count": late_rows,
        "question_count": len(q_ids),
        "students_with_any_submission": len(students_with_any),
        "students_fully_complete": len(students_all_done),
        "question_stats": question_stats,
        "question_stats_by_id": by_qid,
    }


def compute_question_class_stats(db: Session, question_id: int, _course_id: int, student_ids: Iterable[int]) -> dict:
    """Snapshot-based distribution for one question (gradebook effective scores)."""
    sid_list = list(student_ids)
    scores: list[Decimal] = []
    source_counts: dict[str, int] = defaultdict(int)
    if sid_list:
        rows = db.execute(
            select(FinalGradeSnapshot.score, FinalGradeSnapshot.feedback_source).where(
                FinalGradeSnapshot.question_id == question_id,
                FinalGradeSnapshot.student_id.in_(sid_list),
            )
        ).all()
        for score, fsrc in rows:
            if fsrc is not None:
                raw = fsrc.value if hasattr(fsrc, "value") else str(fsrc)
                source_counts[raw] += 1
            if score is not None:
                scores.append(score if isinstance(score, Decimal) else Decimal(str(score)))

    submitted_set: set[int] = set()
    if sid_list:
        submitted_set = set(
            int(r[0])
            for r in db.execute(
                select(Submission.user_id)
                .where(Submission.question_id == question_id, Submission.user_id.in_(sid_list))
                .distinct()
            ).all()
        )
    n_students = len(sid_list)
    missing = []
    for sid in sid_list:
        if sid not in submitted_set:
            u = db.get(User, sid)
            missing.append({"id": sid, "username": u.username if u else str(sid)})

    stats = _score_stats(scores)
    q = db.get(Question, question_id)
    max_score = float(q.max_score) if q is not None else None
    return {
        "active_student_count": n_students,
        "submitted_any_count": len(submitted_set),
        "missing_count": max(0, n_students - len(submitted_set)),
        "missing_students": missing[:200],
        "missing_truncated": max(0, n_students - len(submitted_set) - 200),
        "score_stats": stats,
        "max_score": max_score,
        "feedback_source_counts": dict(source_counts) if sid_list else {},
    }


def enrich_course_grade_matrix(db: Session, course_id: int, matrix: dict) -> dict:
    """Add per-cell completion (x/y questions) and full-completion flag to summarize_course_grade_matrix output."""
    rows = matrix.get("rows") or []
    assignments = matrix.get("assignments") or []
    if not rows or not assignments:
        return matrix

    student_ids = [int(r["student"]["id"]) for r in rows]
    assignment_ids = [int(a["id"]) for a in assignments]
    q_counts = question_counts_by_assignment(db, course_id)
    dist = distinct_questions_submitted_by_pair(db, course_id, student_ids)

    for r in rows:
        sid = int(r["student"]["id"])
        for i, cell in enumerate(r.get("cells") or []):
            aid = int(assignments[i]["id"])
            q_total = int(q_counts.get(aid, 0))
            n_submitted = int(dist.get((sid, aid), 0))
            cell["questions_submitted"] = n_submitted
            cell["question_total"] = q_total
            cell["fully_complete"] = q_total > 0 and n_submitted >= q_total
            cell["any_submitted"] = n_submitted > 0
    return matrix


def score_distribution_by_question(
    db: Session, question_ids: list[int], student_ids: list[int]
) -> dict[int, list[float]]:
    """question_id -> sorted list of snapshot scores (non-null only)."""
    if not question_ids or not student_ids:
        return {}
    rows = db.execute(
        select(FinalGradeSnapshot.question_id, FinalGradeSnapshot.score).where(
            FinalGradeSnapshot.question_id.in_(question_ids),
            FinalGradeSnapshot.student_id.in_(student_ids),
            FinalGradeSnapshot.score.isnot(None),
        )
    ).all()
    by_q: dict[int, list[float]] = defaultdict(list)
    for qid, score in rows:
        if score is None:
            continue
        v = float(score) if not isinstance(score, Decimal) else float(score)
        by_q[int(qid)].append(v)
    for qid in by_q:
        by_q[qid].sort()
    return dict(by_q)


def grade_summary_from_float_scores(values: list[float]) -> dict:
    """Same shape as _score_stats for display (from unsorted peer scores)."""
    if not values:
        return _score_stats([])
    return _score_stats([Decimal(str(v)) for v in values])


def percentile_rank(sorted_vals: list[float], x: float | None) -> float | None:
    """Fraction of values strictly below x, in [0,1]. None if x is None or no reference."""
    if x is None or not sorted_vals:
        return None
    below = sum(1 for v in sorted_vals if v < x)
    return below / len(sorted_vals)
