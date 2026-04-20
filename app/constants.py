from enum import Enum


class PlatformRole(str, Enum):
    USER = "user"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class AccountRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"


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
    FREE_DISCUSSION_TOPIC = "free_discussion_topic"
    DISCUSSION_GROUP = "discussion_group"


class DiscussionGroupVisibility(str, Enum):
    PRIVATE = "private"
    PUBLIC = "public"


class DiscussionGroupMemberRole(str, Enum):
    OWNER = "owner"
    MEMBER = "member"


class DiscussionGroupStatus(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"


class ReportTargetType(str, Enum):
    USER = "user"
    TEACHING_CARD = "teaching_card"
    FREE_DISCUSSION_CARD = "free_discussion_card"
    DISCUSSION_GROUP_CARD = "discussion_group_card"
    DISCUSSION_POST = "discussion_post"


class ReportStatus(str, Enum):
    PENDING = "pending"
    REVIEWING = "reviewing"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class FriendRequestStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class SocialInviteType(str, Enum):
    COURSE = "course"
    FREE_DISCUSSION_TOPIC = "free_discussion_topic"
    DISCUSSION_GROUP = "discussion_group"


class SocialInviteStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class NotificationType(str, Enum):
    FRIEND_REQUEST = "friend_request"
    FRIEND_ACCEPTED = "friend_accepted"
    DIRECT_MESSAGE = "direct_message"
    COURSE_INVITE = "course_invite"
    FREE_DISCUSSION_TOPIC_INVITE = "free_discussion_topic_invite"
    DISCUSSION_GROUP_INVITE = "discussion_group_invite"


class StorageDeletionActor(str, Enum):
    """Who removed a stored asset (shown to viewers without personal names)."""

    SELF = "self"
    TEACHER = "teacher"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


ACTIVE_SUBMISSION_STATUSES = {SubmissionStatus.QUEUED.value, SubmissionStatus.RUNNING.value}
