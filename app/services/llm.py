import base64
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.constants import LLMProvider, LLMResponseLanguage
from app.models import LLMConfig
from app.services.llm_grading_prompts import language_and_quality_block, truncation_notice_block
from app.services.llm_retry import strip_json_fence


class LLMConnectionTestError(Exception):
    """Raised when a configured LLM provider cannot be reached successfully."""


@dataclass
class LLMTestResult:
    success: bool
    message: str


@dataclass
class LLMGenerationResult:
    content: str
    raw_response: dict | None


@dataclass(frozen=True)
class ImageInput:
    mime_type: str
    data: bytes


def mask_api_key(api_key: str | None) -> str:
    if not api_key:
        return ""
    return "•" * 12


def test_llm_connectivity(config: LLMConfig) -> LLMTestResult:
    if not config.enabled:
        return LLMTestResult(False, "Configuration is disabled.")
    if not config.model_name:
        return LLMTestResult(False, "Model name is required.")
    if config.provider_type in {LLMProvider.OPENAI_COMPATIBLE, LLMProvider.GEMINI, LLMProvider.CLAUDE} and not config.api_key:
        return LLMTestResult(False, "API key is required.")

    try:
        generate_text(config, "Reply with the single word OK.")
    except Exception as exc:
        return LLMTestResult(False, str(exc))
    return LLMTestResult(True, "Provider connectivity test succeeded.")


def test_llm_config_connection(config: LLMConfig) -> str:
    result = test_llm_connectivity(config)
    if not result.success:
        raise LLMConnectionTestError(result.message)
    return result.message


def generate_text(
    config: LLMConfig,
    prompt: str,
    system_prompt: str | None = None,
    *,
    bill_user_id: int | None = None,
    bill_db: Session | None = None,
    bill_user_prompt: str | None = None,
    bill_system_prompt: str | None = None,
) -> LLMGenerationResult:
    if bill_user_id is not None and bill_db is not None:
        from app.services.llm_token_usage import assert_room_for_llm_call, estimate_llm_call_budget, record_llm_usage

        up = bill_user_prompt if bill_user_prompt is not None else prompt
        sp = bill_system_prompt if bill_system_prompt is not None else system_prompt
        est = estimate_llm_call_budget(
            system_prompt=sp,
            user_prompt=up,
            image_count=0,
            multimodal=False,
            image_bytes_total=0,
            max_output_tokens_cap=int(config.max_tokens or 512),
        )
        assert_room_for_llm_call(bill_db, bill_user_id, config, estimated_budget=est)
    if config.provider_type == LLMProvider.OPENAI_COMPATIBLE:
        result = _generate_openai_compatible(config, prompt, system_prompt)
    elif config.provider_type == LLMProvider.GEMINI:
        result = _generate_gemini(config, prompt, system_prompt)
    elif config.provider_type == LLMProvider.CLAUDE:
        result = _generate_claude(config, prompt, system_prompt)
    else:
        raise ValueError(f"Unsupported LLM provider: {config.provider_type.value}")
    if bill_user_id is not None and bill_db is not None:
        from app.services.llm_token_usage import record_llm_usage

        record_llm_usage(bill_db, bill_user_id, config, result.raw_response)
    return result


def generate_multimodal(
    config: LLMConfig,
    *,
    prompt: str,
    system_prompt: str | None = None,
    images: list[ImageInput] | None = None,
    bill_user_id: int | None = None,
    bill_db: Session | None = None,
    bill_image_bytes_total: int | None = None,
) -> LLMGenerationResult:
    image_inputs = images or []
    if bill_user_id is not None and bill_db is not None:
        from app.services.llm_token_usage import assert_room_for_llm_call, estimate_llm_call_budget

        img_bytes = bill_image_bytes_total if bill_image_bytes_total is not None else sum(len(im.data) for im in image_inputs)
        est = estimate_llm_call_budget(
            system_prompt=system_prompt,
            user_prompt=prompt,
            image_count=len(image_inputs),
            multimodal=bool(image_inputs),
            image_bytes_total=img_bytes,
            max_output_tokens_cap=int(config.max_tokens or 512),
        )
        assert_room_for_llm_call(bill_db, bill_user_id, config, estimated_budget=est)
    if config.provider_type == LLMProvider.OPENAI_COMPATIBLE:
        result = _generate_openai_compatible(config, prompt, system_prompt, images=image_inputs)
    elif config.provider_type == LLMProvider.GEMINI:
        result = _generate_gemini(config, prompt, system_prompt, images=image_inputs)
    elif config.provider_type == LLMProvider.CLAUDE:
        result = _generate_claude(config, prompt, system_prompt, images=image_inputs)
    else:
        raise ValueError(f"Unsupported LLM provider: {config.provider_type.value}")
    if bill_user_id is not None and bill_db is not None:
        from app.services.llm_token_usage import record_llm_usage

        record_llm_usage(bill_db, bill_user_id, config, result.raw_response)
    return result


def _response_language_instruction(course_override: str | None, student_submission_text: str) -> str:
    raw = (course_override or LLMResponseLanguage.AUTO.value).strip().lower()
    if raw == LLMResponseLanguage.ZH.value:
        return "Use Chinese (zh)."
    if raw == LLMResponseLanguage.EN.value:
        return "Use English (en)."
    sample = (student_submission_text or "")[:4000]
    if re.search(r"[\u4e00-\u9fff]", sample):
        return "The student's submission appears to use Chinese; use Chinese (zh) unless the rubric clearly requires another language."
    return "The student's submission appears to be primarily non-Chinese; use English (en)."


def _parse_grading_json(raw: str) -> dict:
    cleaned = strip_json_fence(raw)
    parsed = json.loads(cleaned)
    if "score_suggestion" not in parsed or "comment_text" not in parsed:
        raise ValueError("LLM JSON response must include score_suggestion and comment_text.")
    return parsed


def _grading_system_preamble() -> str:
    return (
        "You grade student work. Return JSON only with keys `score_suggestion` (number) and `comment_text` (string). "
        "Do not reveal hidden test code, secret test inputs, or internal staff-only rubric details. "
        "Be concise and actionable in comment_text: strengths, failures, and concrete next steps. "
        "If a previous round is provided, prioritize whether the student addressed the issues raised there."
    )


def generate_short_answer_evaluation(
    config: LLMConfig,
    *,
    question_title: str,
    question_description: str,
    rubric_text: str,
    reference_answer_text: str = "",
    answer_text: str,
    max_score: float,
    previous_submission_text: str = "",
    previous_feedback_text: str = "",
    previous_teacher_score_text: str = "",
    truncation_notice: str = "",
    course_llm_response_language: str | None = None,
    text_format_may_lose_images: bool = False,
    bill_user_id: int | None = None,
    bill_db: Session | None = None,
    images: list[ImageInput] | None = None,
    multimodal_instructions: str = "",
) -> dict:
    lang = _response_language_instruction(course_llm_response_language, answer_text)
    has_images = bool(images)
    quality = language_and_quality_block(
        lang,
        text_submission_may_lose_images=text_format_may_lose_images and not has_images,
        student_submission_is_pdf_pages=False,
    )
    mm_extra = (" " + multimodal_instructions.strip()) if multimodal_instructions.strip() else ""
    system_prompt = _grading_system_preamble() + " " + quality + mm_extra
    prev_block = ""
    if (previous_submission_text or "").strip() or (previous_feedback_text or "").strip():
        prev_block = (
            "\nPrevious graded attempt:\n"
            f"Teacher score on previous attempt: {previous_teacher_score_text or 'Not recorded.'}\n"
            f"Previous submission excerpt:\n{previous_submission_text[:12000]}\n\n"
            f"Previous feedback:\n{previous_feedback_text[:8000]}\n"
        )
    prompt = (
        truncation_notice_block(truncation_notice)
        + f"Question title: {question_title}\n"
        f"Question description:\n{question_description}\n\n"
        f"Reference answer:\n{reference_answer_text or 'No reference answer provided.'}\n\n"
        f"Rubric:\n{rubric_text or 'No explicit rubric provided.'}\n\n"
        f"Maximum score: {max_score}\n\n"
        f"Student answer:\n{answer_text}\n"
        + prev_block
        + "\nReturn valid JSON only."
    )
    if images:
        raw = generate_multimodal(
            config,
            prompt=prompt,
            system_prompt=system_prompt,
            images=images,
            bill_user_id=bill_user_id,
            bill_db=bill_db,
            bill_image_bytes_total=sum(len(image.data) for image in images),
        ).content
    else:
        raw = generate_text(
            config,
            prompt,
            system_prompt,
            bill_user_id=bill_user_id,
            bill_db=bill_db,
            bill_user_prompt=prompt,
            bill_system_prompt=system_prompt,
        ).content
    return _parse_grading_json(raw)


def generate_file_evaluation_from_images(
    config: LLMConfig,
    *,
    question_title: str,
    question_description: str,
    rubric_text: str,
    reference_answer_text: str,
    max_score: float,
    images: list[ImageInput],
    previous_submission_text: str = "",
    previous_feedback_text: str = "",
    previous_teacher_score_text: str = "",
    truncation_notice: str = "",
    course_llm_response_language: str | None = None,
    bill_user_id: int | None = None,
    bill_db: Session | None = None,
) -> dict:
    if not images:
        raise ValueError("At least one rendered PDF page image is required for multimodal grading.")
    lang = _response_language_instruction(course_llm_response_language, reference_answer_text)
    quality = language_and_quality_block(
        lang,
        text_submission_may_lose_images=False,
        student_submission_is_pdf_pages=True,
    )
    system_prompt = _grading_system_preamble() + " " + quality
    prev_block = ""
    if (previous_submission_text or "").strip() or (previous_feedback_text or "").strip():
        prev_block = (
            "\nPrevious graded attempt:\n"
            f"Teacher score on previous attempt: {previous_teacher_score_text or 'Not recorded.'}\n"
            f"Previous submission excerpt:\n{previous_submission_text[:12000]}\n\n"
            f"Previous feedback:\n{previous_feedback_text[:8000]}\n"
        )
    prompt = (
        truncation_notice_block(truncation_notice)
        + f"Question title: {question_title}\n"
        f"Question description:\n{question_description}\n\n"
        f"Reference answer:\n{reference_answer_text or 'No reference answer provided.'}\n\n"
        f"Rubric:\n{rubric_text or 'No explicit rubric provided.'}\n\n"
        f"Maximum score: {max_score}\n\n"
        "The student's PDF has been rendered into page images attached to this request. "
        "Review the pages and return valid JSON only."
        + prev_block
    )
    raw = generate_multimodal(
        config,
        prompt=prompt,
        system_prompt=system_prompt,
        images=images,
        bill_user_id=bill_user_id,
        bill_db=bill_db,
    ).content
    return _parse_grading_json(raw)


def _generate_openai_compatible(
    config: LLMConfig,
    prompt: str,
    system_prompt: str | None,
    images: list[ImageInput] | None = None,
) -> LLMGenerationResult:
    if not config.base_url:
        raise ValueError("Base URL is required for OpenAI-compatible providers.")
    endpoint = config.base_url.rstrip("/") + "/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if images:
        content_parts: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"
                    },
                }
            )
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": prompt})
    payload = {
        "model": config.model_name,
        "messages": messages,
        "temperature": _temperature_value(config.temperature),
        "max_tokens": config.max_tokens,
    }
    response = _post_json(
        endpoint,
        payload,
        {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        timeout=config.timeout_seconds,
    )
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("OpenAI-compatible provider returned no choices.")
    content = choices[0].get("message", {}).get("content")
    if isinstance(content, list):
        content = "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        )
    if not content:
        raise ValueError("OpenAI-compatible provider returned an empty response.")
    return LLMGenerationResult(content=content.strip(), raw_response=response)


def _generate_gemini(
    config: LLMConfig,
    prompt: str,
    system_prompt: str | None,
    images: list[ImageInput] | None = None,
) -> LLMGenerationResult:
    base_url = config.base_url.rstrip("/") if config.base_url else "https://generativelanguage.googleapis.com"
    endpoint = (
        f"{base_url}/v1beta/models/{config.model_name}:generateContent?key={config.api_key}"
    )
    prompt_text = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    parts: list[dict] = [{"text": prompt_text}]
    for image in images or []:
        parts.append(
            {
                "inline_data": {
                    "mime_type": image.mime_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                }
            }
        )
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": _temperature_value(config.temperature),
            "maxOutputTokens": config.max_tokens,
        },
    }
    response = _post_json(endpoint, payload, {"Content-Type": "application/json"}, timeout=config.timeout_seconds)
    candidates = response.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidates.")
    parts = candidates[0].get("content", {}).get("parts") or []
    text_parts = [part.get("text", "") for part in parts if part.get("text")]
    content = "\n".join(text_parts).strip()
    if not content:
        raise ValueError("Gemini returned an empty response.")
    return LLMGenerationResult(content=content, raw_response=response)


def _generate_claude(
    config: LLMConfig,
    prompt: str,
    system_prompt: str | None,
    images: list[ImageInput] | None = None,
) -> LLMGenerationResult:
    if not config.base_url:
        raise ValueError("Base URL is required for Claude providers.")
    endpoint = config.base_url.rstrip("/") + "/messages"
    message_content: list[dict] = [{"type": "text", "text": prompt}]
    for image in images or []:
        media_type = image.mime_type.split("/", 1)[-1].lower()
        message_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.mime_type if image.mime_type.startswith("image/") else f"image/{media_type}",
                    "data": base64.b64encode(image.data).decode("ascii"),
                },
            }
        )
    payload = {
        "model": config.model_name,
        "max_tokens": config.max_tokens,
        "temperature": _temperature_value(config.temperature),
        "messages": [{"role": "user", "content": message_content}],
    }
    if system_prompt:
        payload["system"] = system_prompt
    response = _post_json(
        endpoint,
        payload,
        {
            "x-api-key": config.api_key or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        timeout=config.timeout_seconds,
    )
    blocks = response.get("content") or []
    text_parts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
    content = "\n".join(part for part in text_parts if part).strip()
    if not content:
        raise ValueError("Claude returned an empty response.")
    return LLMGenerationResult(content=content, raw_response=response)


def _post_json(url: str, payload: dict, headers: dict[str, str], timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = Request(url=url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8")
            return json.loads(raw_text)
    except HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"HTTP {exc.code}: {error_text}") from exc
    except URLError as exc:
        raise ValueError(f"Network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Provider response was not valid JSON.") from exc


def _temperature_value(raw_value: str | None) -> float:
    try:
        return float(raw_value or "0.2")
    except ValueError:
        return 0.2
