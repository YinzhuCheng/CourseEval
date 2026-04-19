# Architecture: scoring pipeline

## Purpose

Describe how a **numeric score** visible to students/teachers is derived from **automatic evaluation**, **LLM suggestions**, and **teacher feedback**, and how **gradebook snapshots** stay consistent.

## Key concepts

| Concept | Definition / location |
|---------|------------------------|
| `Feedback` | Rows tied to `submission_id`, with `source` ∈ {`auto`, `llm`, `teacher`} and optional `score_suggestion`. |
| `EvaluationResult` | Holds `auto_score`, `visible_score`, `hidden_score`, `final_score`, and `summary_json` from code runner. |
| `FinalGradeSnapshot` | One row per (student, question) for gradebook / analytics; updated by `update_final_grade_snapshot`. |

Models: `app/models.py`. Effective score logic: `app/services/scoring.py`. Snapshot recompute: `app/services/submissions.py`.

## Resolution order (student-facing effective score)

**Authoritative for submission UI:** `resolve_submission_score` in `app/services/scoring.py`.

Order (simplified—read function body for edge cases):

1. Latest **teacher** `Feedback` with score → that score wins.
2. Else latest `EvaluationResult.final_score` (auto-run numeric).
3. Else latest **LLM** `Feedback` score **unless** `submission_requires_teacher_confirmation(submission)` is true—in that strict mode, LLM score is suppressed until teacher feedback exists.
4. Else latest **auto** `Feedback` from code evaluation path.

Related helpers in the same file:

- `submission_requires_teacher_confirmation` — reads `short_answer_config` or `file_question_config` flags.
- `submission_eligible_for_gradebook` — includes edge case where strict mode + LLM exists but teacher has not scored yet.
- `is_submission_pending_teacher_review` — drives warning banners in templates.

## Gradebook snapshot

**Function:** `update_final_grade_snapshot(db, submission)` in `app/services/submissions.py`.

**Behavior sketch:**

- Loads all submissions for same `(user, question)` eligible for gradebook.
- Applies assignment/question **scoring rule** (`latest` vs `highest`) or historical-highest flag stored on snapshot.
- Writes `FinalGradeSnapshot.effective_submission_id`, `score`, `feedback_source`, etc.

**Consumers:** Teacher analytics (`app/services/teacher_analytics.py`), post-close reveal (`app/services/post_close_reveal.py`), teacher UI queries in `app/routes/teacher.py`.

## Teacher grading HTTP

**Route:** `grade_submission` (POST) in `app/routes/teacher.py` — creates teacher `Feedback`, then should trigger snapshot recompute (grep `update_final_grade_snapshot` in that flow).

## Single Score Resolver

There should be exactly one `resolve_submission_score` definition in the repository: `app/services/scoring.py`.

Analytics, routes, and submission views should import that resolver or the related teacher-review helpers instead of duplicating priority rules.

## Templates & student view

`build_student_result_view` in `submissions.py` prepares a dict for `student_submission_detail.html`.

**Also check:** `app/enum_labels.py` for `feedback_source` display strings.

## Tests

- `tests/test_teacher_analytics.py` for snapshot-dependent aggregates.
- Grep `FinalGradeSnapshot` in `tests/` for broader coverage.

## Common misconceptions

- Assuming **LLM score always applies** — blocked when strict teacher confirmation is enabled on the question config.
- Assuming **`EvaluationResult.auto_score` is always the displayed score** — teacher/LLM feedback may override per `resolve_submission_score`.

## Source of truth

1. `app/services/scoring.py` — `resolve_submission_score`, confirmation flags.
2. `app/services/submissions.py` — `update_final_grade_snapshot`.
3. `app/routes/teacher.py` — teacher feedback persistence.
4. `app/models.py` — schema for `Feedback`, `EvaluationResult`, `FinalGradeSnapshot`.

## Coordinated changes

See `docs/change-guide.md` → *Scoring logic & gradebook snapshots*.
