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


def mask_api_key(api_key: str | None) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


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


def _generate_openai_compatible(config: LLMConfig, prompt: str, system_prompt: str | None) -> LLMGenerationResult:
    if not config.base_url:
        raise ValueError("Base URL is required for OpenAI-compatible providers.")
    endpoint = config.base_url.rstrip("/") + "/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
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
    if not content:
        raise ValueError("OpenAI-compatible provider returned an empty response.")
    return LLMGenerationResult(content=content.strip(), raw_response=response)


def _generate_gemini(config: LLMConfig, prompt: str, system_prompt: str | None) -> LLMGenerationResult:
    base_url = config.base_url.rstrip("/") if config.base_url else "https://generativelanguage.googleapis.com"
    endpoint = (
        f"{base_url}/v1beta/models/{config.model_name}:generateContent?key={config.api_key}"
    )
    prompt_text = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
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


def _generate_claude(config: LLMConfig, prompt: str, system_prompt: str | None) -> LLMGenerationResult:
    if not config.base_url:
        raise ValueError("Base URL is required for Claude providers.")
    endpoint = config.base_url.rstrip("/") + "/messages"
    payload = {
        "model": config.model_name,
        "max_tokens": config.max_tokens,
        "temperature": _temperature_value(config.temperature),
        "messages": [{"role": "user", "content": prompt}],
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
