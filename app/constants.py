from enum import Enum


class PlatformRole(str, Enum):
    USER = "user"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class AccountRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    # Legacy value kept for smooth migrations from the shared-registration-code flow.
    ADMINISTRATOR = "administrator"


class UserRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class Locale(str, Enum):
    EN = "en"
    ZH = "zh"


class CourseStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class CourseRole(str, Enum):
    TEACHER = "teacher"
    STUDENT = "student"
    TA = "ta"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    REMOVED = "removed"


class AssignmentStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CLOSED = "closed"
    ARCHIVED = "archived"


class QuestionType(str, Enum):
    NOTEBOOK = "notebook"
    SHORT_ANSWER = "short_answer"
    PYTHON_CODE = "python_code"
    PDF_LLM = "pdf_llm"
    FORMATTED_TEXT_LLM = "formatted_text_llm"


class ScoringRule(str, Enum):
    LATEST = "latest"
    HIGHEST = "highest"


class SubmissionLimitMode(str, Enum):
    UNLIMITED = "unlimited"
    DAILY = "daily"
    TOTAL = "total"


class SubmissionStatus(str, Enum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED_SYSTEM = "failed_system"
    FAILED_ANSWER = "failed_answer"


class EvaluationTaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvaluationTaskType(str, Enum):
    NOTEBOOK_EVALUATION = "notebook_evaluation"
    SHORT_ANSWER_LLM = "short_answer_llm"
    NOTEBOOK_LLM_FEEDBACK = "notebook_llm_feedback"
    PYTHON_CODE_EVALUATION = "python_code_evaluation"
    FILE_LLM_EVALUATION = "file_llm_evaluation"


class FeedbackSource(str, Enum):
    AUTO = "auto"
    LLM = "llm"
    TEACHER = "teacher"


class RuntimeScope(str, Enum):
    PLATFORM = "platform"
    COURSE = "course"


class LLMProvider(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"
    GEMINI = "gemini"
    CLAUDE = "claude"


class LLMScope(str, Enum):
    PLATFORM = "platform"
    COURSE = "course"


class LLMTestStatus(str, Enum):
    NEVER = "never"
    SUCCESS = "success"
    FAILED = "failed"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


ACTIVE_JOB_STATUSES = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
ACTIVE_SUBMISSION_STATUSES = {SubmissionStatus.QUEUED.value, SubmissionStatus.RUNNING.value}
