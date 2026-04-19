# Known issues and review notes

This page records confirmed repository risks that are easy to miss during code review. These are not feature requests; they are maintenance notes for future changes.

If a note here disagrees with code, trust the code and update this page in the same change.

---

## Confirmed notes from the current review

### Two `resolve_submission_score` functions

There are two functions named `resolve_submission_score`:

- `app/services/submissions.py`: `resolve_submission_score(submission)`
- `app/services/courses.py`: `resolve_submission_score(submission, latest_feedback)`

They have different signatures and different priority rules. The `submissions.py` version is authoritative for student submission detail, teacher-confirmation gating, and final grade snapshots. The `courses.py` version is used by course/listing-style analytics helpers and does not apply the strict teacher-confirmation gate in the same way.

When changing scoring behavior, grep both functions and their callers. A one-file scoring change can produce mismatched student UI, teacher analytics, and gradebook snapshots.

### Migration strategy is intentionally lightweight

There is no Alembic migration tree in the repository. Startup calls `init_database()` from `app/main.py`, which runs `Base.metadata.create_all()` and a set of hand-written compatibility steps in `app/db.py` (`migrate_legacy_schema`, `_ensure_column`, enum normalization, backfills).

This is suitable for small deployments and local upgrades, but it is not a general production migration framework. Before a schema-changing deployment, inspect `app/db.py`, test the upgrade against a copy of the production database, and document any manual migration steps.

### Permission tests are scattered

Access control is enforced by explicit route-level calls to helpers in `app/services/permissions.py` and `app/auth.py`. There is no centralized policy engine or one dedicated permission test suite.

When reviewing permission changes, grep for `require_`, `can_`, route handlers, and template controls that expose the action. Tests are often integration-style and spread across route or subsystem tests.

### Legacy notebook naming remains in active code

Standalone notebook execution and `/jobs/*` UI are retired, but `notebook` names still appear in models, task types, queue defaults, config names, templates, and compatibility helpers.

Current supported `.ipynb` work is file/LLM-style review. Do not infer behavior from names like `Notebook`, `Job`, `QuestionType.NOTEBOOK`, or `notebook_question_configs`; inspect `app/services/submissions.py`, `app/routes/jobs.py`, and the question type being handled.

---

## Similar issues found in the repository

### Unified file/LLM questions coexist with legacy enum values

The current teacher UI creates unified file upload / LLM-reviewed questions as `QuestionType.FILE_LLM`. Legacy enum values `PDF_LLM` and `FORMATTED_TEXT_LLM` still exist in `app/constants.py`, templates, and service branches. `app/db.py` backfills old rows to `file_llm`.

When changing file-question behavior, include all three values in greps:

- `file_llm`
- `pdf_llm`
- `formatted_text_llm`

Also inspect `FileQuestionConfig.accepted_extensions`, because PDF, TeX, text, and ipynb behavior is now controlled more by allowed extensions than by the old split question types.

### PDF grading behavior is image-first

The code path for PDF submissions renders pages to PNG images and sends them to a multimodal LLM. Reference-answer PDFs may still be text-extracted for prompt context.

If you update PDF behavior or wording, inspect:

- `create_file_submission` and `process_file_llm_evaluation` in `app/services/submissions.py`
- student and teacher question templates
- README deployment notes about multimodal-capable models

Avoid reintroducing copy that says scanned/image PDFs are unsupported unless the code changes to enforce that.

### LLM configuration is selected differently by grading and discussion AI

Grading uses `_resolve_llm_config_for_question` in `app/services/submissions.py` with question, assignment, course, then platform default precedence.

Discussion AI uses `resolve_discussion_ai_llm_config` in `app/services/discussion_ai.py` with course discussion overrides, platform discussion overrides, then platform default/latest tested config.

These paths intentionally share `LLMConfig` rows but not the same precedence rules. When changing LLM selection, quota, or admin UI wording, check both paths and tests.

### Data-path helpers are duplicated

`relative_to_data` and `absolute_data_path` exist in both `app/services/submissions.py` and `app/services/course_materials.py` with similar safety checks.

If path security, upload layout, or data-directory behavior changes, update both implementations or factor them into a shared helper with focused tests. Do not weaken the "path must remain inside data dir" invariant.

### Queue names include retired terminology

`RQ_QUEUE_NAME` and the default `notebook-jobs` setting remain in `app/config.py` for compatibility, but current enqueue/worker code uses:

- `PYTHON_QUEUE_NAME`
- `LLM_QUEUE_PREFIX`
- `get_python_queue_name`
- `llm_queue_name_for_config`

For worker deployment or queue debugging, use the current queue names. Treat `RQ_QUEUE_NAME` as legacy unless a caller is reintroduced.

### Question version snapshots are not a full audit log

`QuestionVersion` stores serialized question/config snapshots and is used to associate submissions with the question version at submission time. It is not a full event-sourced history of every teacher action.

When changing question config fields, update `app/services/question_versions.py` serialization/restoration and any schema/backfill code. Otherwise restored versions, gradebook rows, and newly edited questions can diverge.
