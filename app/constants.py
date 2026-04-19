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
    SHORT_ANSWER = "short_answer"
    CODE = "code"
    FILE_LLM = "file_llm"


class CodeLanguage(str, Enum):
    PYTHON = "python"
    C = "c"
    CPP = "cpp"


class CodeSubmissionMode(str, Enum):
    SINGLE_FILE = "single_file"
    ZIP = "zip"


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
    SHORT_ANSWER_LLM = "short_answer_llm"
    CODE_EVALUATION = "code_evaluation"
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


class LLMResponseLanguage(str, Enum):
    AUTO = "auto"
    ZH = "zh"
    EN = "en"


class DiscussionTopicKind(str, Enum):
    COURSE_MATERIAL = "course_material"
    QUESTION = "question"


ACTIVE_SUBMISSION_STATUSES = {SubmissionStatus.QUEUED.value, SubmissionStatus.RUNNING.value}
