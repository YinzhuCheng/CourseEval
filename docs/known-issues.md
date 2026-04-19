# Known issues and review notes

This page records confirmed repository risks that are easy to miss during code review. These are not feature requests; they are maintenance notes for future changes.

If a note here disagrees with code, trust the code and update this page in the same change.

---

## Confirmed notes from the current review

### Score resolution has one owner

`resolve_submission_score` now has a single definition in `app/services/scoring.py`. Keep it that way. If a route, analytics helper, or template needs effective score semantics, import from `scoring.py` instead of recreating the priority rules.

### Migration strategy is intentionally lightweight v0

There is no Alembic migration tree in the repository. Startup calls `init_database()` from `app/main.py`, which runs `Base.metadata.create_all()` and seeds the open community course.

This v0 baseline intentionally does not keep removed notebook/job tables, old compatibility aliases, or old file-question enum shims. Before a schema-changing deployment with real data, add a documented migration script or explicit idempotent upgrade step and test it against a database copy.

### Permission tests are scattered

Access control is enforced by explicit route-level calls to helpers in `app/services/permissions.py` and `app/auth.py`. There is no centralized policy engine or one dedicated permission test suite.

When reviewing permission changes, grep for `require_`, `can_`, route handlers, and template controls that expose the action. Tests are often integration-style and spread across route or subsystem tests.

### Notebook wording means file/LLM notebook uploads

Standalone notebook execution UI and job models are not part of v0. Remaining notebook wording refers to `.ipynb` files handled by `file_llm` and `app/services/notebook_multimodal.py`.

Do not reintroduce a notebook question type unless the product is explicitly adding a new active workflow with tests and migration notes.

---

## Similar issues found in the repository

### File/LLM depends on extension-specific extraction

The teacher UI creates unified file upload / LLM-reviewed questions as `QuestionType.FILE_LLM`. PDF, text, TeX, Markdown, and ipynb behavior is controlled by `FileQuestionConfig.accepted_extensions` and the uploaded file extension.

When changing file-question behavior, inspect:

- `create_file_submission`
- `process_file_llm_evaluation`
- `FileQuestionConfig.accepted_extensions`
- `app/services/notebook_multimodal.py`
- student and teacher question templates

### PDF grading behavior is image-first

The code path for PDF submissions renders pages to PNG images and sends them to a multimodal LLM. Reference-answer PDFs may still be text-extracted for prompt context.

If you update PDF behavior or wording, inspect:

- `create_file_submission` and `process_file_llm_evaluation` in `app/services/submissions.py`
- student and teacher question templates
- README deployment notes about multimodal-capable models

Avoid reintroducing copy that says scanned/image PDFs are unsupported unless the code changes to enforce that.

### LLM group selection is shared but precedence differs by workflow

`LLMConfig` is now the group-level record. Its inline provider settings are priority #1, and additional fallback members live in `LLMConfigMember` rows. A callable group needs at least one enabled member or priority #1 entry with successful connectivity testing.

Grading uses `_resolve_llm_config_for_question` in `app/services/submissions.py` with question, assignment, course, then platform default precedence. Discussion AI uses `resolve_discussion_ai_llm_config` in `app/services/discussion_ai.py` with course discussion overrides, platform discussion overrides, then platform default/latest tested group; user `@AI` requests may also pass a selected group.

Within a selected group, calls start from priority #1 and only try later members when earlier members fail. A later task starts again from #1.

### Data-path helpers are centralized

`relative_to_data`, `absolute_data_path`, and writable-directory helpers live in `app/services/storage_paths.py`.

If path security, upload layout, or data-directory behavior changes, update the shared helper and all callers. Do not weaken the "path must remain inside data dir" invariant.

### Queue names are explicit

Current enqueue/worker code uses:

- `CODE_QUEUE_NAME`
- `LLM_QUEUE_PREFIX`
- `get_code_queue_name`
- `llm_queue_name_for_config`

Do not add a generic queue setting unless a new worker topology requires it.

### Question version snapshots are not a full audit log

`QuestionVersion` stores serialized question/config snapshots and is used to associate submissions with the question version at submission time. It is not a full event-sourced history of every teacher action.

When changing question config fields, update `app/services/question_versions.py` serialization and any schema/backfill code. Otherwise gradebook rows and newly edited questions can diverge.
