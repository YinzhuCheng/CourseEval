from enum import Enum


class PlatformRole(str, Enum):
    USER = "user"
    ADMIN = "admin"


class AccountRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMINISTRATOR = "administrator"


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
