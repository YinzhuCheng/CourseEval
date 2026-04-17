"""Shared instructions for structured LLM grading (JSON: score_suggestion + comment_text)."""

from __future__ import annotations


def language_and_quality_block(
    response_language_instruction: str,
    *,
    text_submission_may_lose_images: bool = False,
    student_submission_is_pdf_pages: bool = False,
) -> str:
    parts = [
        "Response language: "
        + response_language_instruction.strip()
        + " Write `comment_text` in that language.",
    ]
    if text_submission_may_lose_images:
        parts.append(
            "The student's submission was converted to plain text from TeX, notebook (ipynb), or similar; "
            "embedded images and some layout may be missing. Infer cautiously where visuals matter; "
            "state uncertainty rather than inventing chart contents. Prefer comparing logic and text to the reference answer."
        )
    if student_submission_is_pdf_pages:
        parts.append(
            "The student's submission is provided as rendered PDF page images (multimodal). "
            "The reference answer is plain text and may describe figures or tables in words—use it as the ground truth for intent."
        )
    return " ".join(parts)


def truncation_notice_block(notice: str) -> str:
    if not (notice or "").strip():
        return ""
    return f"\n[System note: {notice.strip()}]\n"
