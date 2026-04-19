"""Localized display labels for persisted enum string values (user-facing)."""

from __future__ import annotations

LABELS: dict[str, dict[str, dict[str, str]]] = {
    "en": {
        "assignment_status": {
            "draft": "Draft",
            "published": "Published",
            "closed": "Closed",
            "archived": "Archived",
        },
        "scoring_rule": {"latest": "Latest attempt", "highest": "Highest score"},
        "submission_limit_mode": {"daily": "Per day", "total": "Total cap", "unlimited": "No cap"},
        "question_type": {
            "notebook": "Notebook (.ipynb) / LLM",
            "short_answer": "Short answer",
            "code": "Code question",
            "pdf_llm": "PDF / LLM",
            "formatted_text_llm": "Formatted text / LLM",
            "file_llm": "File upload / LLM",
        },
        "submission_status": {
            "submitted": "Submitted",
            "queued": "Queued",
            "running": "Running",
            "completed": "Completed",
            "failed_system": "Failed (system)",
            "failed_answer": "Failed (answer)",
        },
        "course_role": {"student": "Student", "teacher": "Teacher", "ta": "TA"},
        "course_status": {"active": "Active", "archived": "Archived"},
        "membership_status": {"active": "Active", "removed": "Removed"},
        "feedback_source": {"teacher": "Teacher", "llm": "LLM", "auto": "Auto"},
        "evaluation_task_status": {
            "queued": "Queued",
            "running": "Running",
            "succeeded": "Succeeded",
            "failed": "Failed",
        },
        "llm_provider": {
            "openai_compatible": "OpenAI-compatible",
            "gemini": "Gemini",
            "claude": "Claude",
        },
        "llm_test_status": {"never": "Never tested", "success": "OK", "failed": "Failed"},
        "job_status": {"queued": "Queued", "running": "Running", "success": "Success", "failed": "Failed"},
        "runtime_scope": {"platform": "Platform", "course": "Course"},
    },
    "zh": {
        "assignment_status": {"draft": "草稿", "published": "已发布", "closed": "已关闭", "archived": "已归档"},
        "scoring_rule": {"latest": "按最后一次", "highest": "按最高分"},
        "submission_limit_mode": {"daily": "每日上限", "total": "总次数上限", "unlimited": "不限制"},
        "question_type": {
            "notebook": "Notebook（.ipynb）/ LLM",
            "short_answer": "简答题",
            "code": "代码题",
            "pdf_llm": "PDF / LLM",
            "formatted_text_llm": "格式化文本 / LLM",
            "file_llm": "文件上传 / LLM",
        },
        "submission_status": {
            "submitted": "已提交",
            "queued": "排队中",
            "running": "运行中",
            "completed": "已完成",
            "failed_system": "失败（系统）",
            "failed_answer": "失败（作答）",
        },
        "course_role": {"student": "学生", "teacher": "教师", "ta": "助教"},
        "course_status": {"active": "进行中", "archived": "已归档"},
        "membership_status": {"active": "在册", "removed": "已移除"},
        "feedback_source": {"teacher": "教师", "llm": "大模型", "auto": "自动"},
        "evaluation_task_status": {
            "queued": "排队中",
            "running": "运行中",
            "succeeded": "成功",
            "failed": "失败",
        },
        "llm_provider": {
            "openai_compatible": "OpenAI 兼容",
            "gemini": "Gemini",
            "claude": "Claude",
        },
        "llm_test_status": {"never": "未测试", "success": "通过", "failed": "失败"},
        "job_status": {"queued": "排队中", "running": "运行中", "success": "成功", "failed": "失败"},
        "runtime_scope": {"platform": "平台", "course": "课程"},
    },
}


def label_for_enum(locale: str, category: str, value: str | None) -> str:
    if value is None:
        return "-"
    v = str(value).strip().lower()
    catalog = LABELS.get(locale if locale in LABELS else "en", {})
    bucket = catalog.get(category, {})
    return bucket.get(v, str(value))
