import base64
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.constants import LLMProvider
from app.models import LLMConfig


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


def generate_text(config: LLMConfig, prompt: str, system_prompt: str | None = None) -> LLMGenerationResult:
    if config.provider_type == LLMProvider.OPENAI_COMPATIBLE:
        return _generate_openai_compatible(config, prompt, system_prompt)
    if config.provider_type == LLMProvider.GEMINI:
        return _generate_gemini(config, prompt, system_prompt)
    if config.provider_type == LLMProvider.CLAUDE:
        return _generate_claude(config, prompt, system_prompt)
    raise ValueError(f"Unsupported LLM provider: {config.provider_type.value}")


def generate_multimodal(
    config: LLMConfig,
    *,
    prompt: str,
    system_prompt: str | None = None,
    images: list[ImageInput] | None = None,
) -> LLMGenerationResult:
    image_inputs = images or []
    if config.provider_type == LLMProvider.OPENAI_COMPATIBLE:
        return _generate_openai_compatible(config, prompt, system_prompt, images=image_inputs)
    if config.provider_type == LLMProvider.GEMINI:
        return _generate_gemini(config, prompt, system_prompt, images=image_inputs)
    if config.provider_type == LLMProvider.CLAUDE:
        return _generate_claude(config, prompt, system_prompt, images=image_inputs)
    raise ValueError(f"Unsupported LLM provider: {config.provider_type.value}")


def generate_feedback_with_llm(
    config: LLMConfig,
    *,
    question_title: str,
    question_description: str,
    summary_json: str,
    stdout_text: str,
    stderr_text: str,
    auto_score: float,
) -> str:
    system_prompt = (
        "You are a careful teaching assistant. Generate concise, actionable feedback for a notebook programming "
        "submission. Do not reveal hidden test code. Mention strengths, failures, and next steps."
    )
    prompt = (
        f"Question title: {question_title}\n"
        f"Question description:\n{question_description}\n\n"
        f"Automatic score: {auto_score}\n"
        f"Evaluation summary JSON:\n{summary_json}\n\n"
        f"stdout:\n{stdout_text[:8000]}\n\n"
        f"stderr:\n{stderr_text[:8000]}\n\n"
        "Return a short student-facing feedback message."
    )
    return generate_text(config, prompt, system_prompt).content


def generate_notebook_evaluation_with_llm(
    config: LLMConfig,
    *,
    question_title: str,
    question_description: str,
    rubric_text: str,
    summary_json: str,
    stdout_text: str,
    stderr_text: str,
    max_llm_score: float,
) -> dict:
    system_prompt = (
        "You are grading a notebook programming submission. Return JSON only with keys "
        "`score_suggestion` and `comment_text`. "
        "score_suggestion must be a number between 0 and the provided maximum score."
    )
    prompt = (
        f"Question title: {question_title}\n"
        f"Question description:\n{question_description}\n\n"
        f"Notebook grading rubric:\n{rubric_text or 'No explicit rubric provided.'}\n\n"
        f"Maximum LLM score: {max_llm_score}\n\n"
        f"Evaluation summary JSON:\n{summary_json}\n\n"
        f"stdout:\n{stdout_text[:8000]}\n\n"
        f"stderr:\n{stderr_text[:8000]}\n\n"
        "Return valid JSON only."
    )
    raw = generate_text(config, prompt, system_prompt).content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM did not return valid JSON: {raw}") from exc
    if "score_suggestion" not in parsed or "comment_text" not in parsed:
        raise ValueError("LLM JSON response must include score_suggestion and comment_text.")
    return parsed


def generate_short_answer_evaluation(
    config: LLMConfig,
    *,
    question_title: str,
    question_description: str,
    rubric_text: str,
    reference_answer_text: str = "",
    answer_text: str,
    max_score: float,
) -> dict:
    system_prompt = (
        "You are grading a student's short-answer response. Produce a JSON object with keys "
        "`score_suggestion` and `comment_text`. The score must be between 0 and the maximum score."
    )
    prompt = (
        f"Question title: {question_title}\n"
        f"Question description:\n{question_description}\n\n"
        f"Reference answer:\n{reference_answer_text or 'No reference answer provided.'}\n\n"
        f"Rubric:\n{rubric_text or 'No explicit rubric provided.'}\n\n"
        f"Maximum score: {max_score}\n\n"
        f"Student answer:\n{answer_text}\n\n"
        "Return valid JSON only."
    )
    raw = generate_text(config, prompt, system_prompt).content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM did not return valid JSON: {raw}") from exc
    if "score_suggestion" not in parsed or "comment_text" not in parsed:
        raise ValueError("LLM JSON response must include score_suggestion and comment_text.")
    return parsed


def generate_file_evaluation_from_images(
    config: LLMConfig,
    *,
    question_title: str,
    question_description: str,
    rubric_text: str,
    reference_answer_text: str,
    max_score: float,
    images: list[ImageInput],
) -> dict:
    if not images:
        raise ValueError("At least one rendered PDF page image is required for multimodal grading.")
    system_prompt = (
        "You are grading a student's PDF submission from rendered page images. "
        "Produce a JSON object with keys `score_suggestion` and `comment_text`. "
        "The score must be between 0 and the maximum score."
    )
    prompt = (
        f"Question title: {question_title}\n"
        f"Question description:\n{question_description}\n\n"
        f"Reference answer:\n{reference_answer_text or 'No reference answer provided.'}\n\n"
        f"Rubric:\n{rubric_text or 'No explicit rubric provided.'}\n\n"
        f"Maximum score: {max_score}\n\n"
        "The student's PDF has been rendered into page images attached to this request. "
        "Review the pages and return valid JSON only."
    )
    raw = generate_multimodal(config, prompt=prompt, system_prompt=system_prompt, images=images).content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM did not return valid JSON: {raw}") from exc
    if "score_suggestion" not in parsed or "comment_text" not in parsed:
        raise ValueError("LLM JSON response must include score_suggestion and comment_text.")
    return parsed


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
