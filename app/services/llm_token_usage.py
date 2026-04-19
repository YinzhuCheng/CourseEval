"""Per-user daily LLM token limits and usage (calendar day in Asia/Shanghai)."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import LLMProvider
from app.db import utcnow
from app.models import LLMConfig, LLMConfigMember, PlatformLlmTokenPolicy, User, UserLlmTokenDaily

logger = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")


def beijing_today_str() -> str:
    return datetime.now(BEIJING).date().isoformat()


def effective_daily_token_limit(user: User, platform_default: int) -> int:
    if user.llm_daily_token_limit is not None and user.llm_daily_token_limit > 0:
        return int(user.llm_daily_token_limit)
    return max(1, int(platform_default))


def _get_or_create_platform_default(db: Session) -> int:
    row = db.get(PlatformLlmTokenPolicy, 1)
    if row is None:
        row = PlatformLlmTokenPolicy(id=1, default_user_daily_llm_tokens=100000)
        db.add(row)
        db.commit()
        db.refresh(row)
    return int(row.default_user_daily_llm_tokens)


def get_platform_default_daily_limit(db: Session) -> int:
    return _get_or_create_platform_default(db)


def estimate_llm_call_budget(
    *,
    system_prompt: str | None,
    user_prompt: str,
    image_count: int = 0,
    multimodal: bool = False,
    image_bytes_total: int = 0,
    max_output_tokens_cap: int | None = None,
) -> int:
    """Rough pre-call token budget from input size (not actual API usage).

    Used only for daily-quota *pre-checks*. Actual usage is recorded separately
    from provider ``usage`` after a successful call.
    """
    system_text = system_prompt or ""
    prompt_text = user_prompt or ""
    char_len = len(system_text) + len(prompt_text)
    # Mixed text: ~4 chars per token (conservative); small fixed overhead for message framing.
    text_tokens = 400 + (char_len + 3) // 4

    vision_tokens = 0
    if multimodal and image_count > 0:
        avg_bytes = max(1, image_bytes_total // max(1, image_count))
        # Vision-style inputs: base per image + mild size bump (capped per image).
        per_image = 1500 + min(24_000, avg_bytes // 60)
        vision_tokens = image_count * per_image

    # Possible completion tokens: bounded by API max output setting, not multiplied.
    cap = int(max_output_tokens_cap) if max_output_tokens_cap is not None else 4096
    completion_upper = min(32_768, max(256, cap))

    return min(500_000, int(text_tokens + vision_tokens + completion_upper))


def extract_total_tokens_from_response(provider: LLMProvider, raw: dict | None) -> int:
    if not raw:
        return 0
    try:
        if provider == LLMProvider.OPENAI_COMPATIBLE:
            usage = raw.get("usage") or {}
            total = usage.get("total_tokens")
            if total is not None:
                return int(total)
            p = int(usage.get("prompt_tokens") or 0)
            c = int(usage.get("completion_tokens") or 0)
            return p + c
        if provider == LLMProvider.GEMINI:
            meta = raw.get("usageMetadata") or {}
            total = meta.get("totalTokenCount")
            if total is not None:
                return int(total)
            return int(meta.get("promptTokenCount") or 0) + int(meta.get("candidatesTokenCount") or 0)
        if provider == LLMProvider.CLAUDE:
            usage = raw.get("usage") or {}
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if inp is not None or out is not None:
                return int(inp or 0) + int(out or 0)
    except (TypeError, ValueError):
        return 0
    return 0


def assert_room_for_llm_call(
    db: Session,
    user_id: int,
    config: LLMConfig | LLMConfigMember,
    *,
    estimated_budget: int,
) -> tuple[int, str, int]:
    """Returns (limit, usage_date, consumed_before). Raises ValueError if quota pre-check fails.

    ``estimated_budget`` is a *rough upper bound for this call* from prompt/image size.
    It is not the same as ``actual_usage`` (recorded after the API returns ``usage``).
    """
    platform_default = _get_or_create_platform_default(db)
    user = db.get(User, user_id)
    if user is None:
        raise ValueError("User not found for LLM token accounting.")
    limit = effective_daily_token_limit(user, platform_default)
    day = beijing_today_str()
    row = db.scalar(select(UserLlmTokenDaily).where(UserLlmTokenDaily.user_id == user_id, UserLlmTokenDaily.usage_date == day))
    consumed = int(row.consumed_tokens) if row else 0
    reserve = max(512, int(estimated_budget))
    remaining = max(0, limit - consumed)
    if consumed >= limit:
        raise ValueError(
            f"Daily LLM token quota exceeded (actual_usage_today={consumed}/{limit} for Beijing date {day}). "
            "Try again tomorrow or ask an administrator to raise your limit."
        )
    if consumed + reserve > limit:
        raise ValueError(
            f"Estimated token budget for this call (~{reserve}) exceeds your remaining daily quota "
            f"(remaining={remaining}, limit={limit}, actual_usage_today={consumed}, Beijing date {day}). "
            "This is a pre-call budget check, not how many tokens you have already used today. "
            "Try again tomorrow, shorten inputs, or ask an administrator to raise your limit."
        )
    return limit, day, consumed


def record_llm_usage(db: Session, user_id: int, config: LLMConfig | LLMConfigMember, raw_response: dict | None) -> None:
    tokens = extract_total_tokens_from_response(config.provider_type, raw_response)
    if tokens <= 0:
        return
    day = beijing_today_str()
    platform_default = _get_or_create_platform_default(db)
    user = db.get(User, user_id)
    if user is None:
        return
    limit = effective_daily_token_limit(user, platform_default)

    row = db.scalar(select(UserLlmTokenDaily).where(UserLlmTokenDaily.user_id == user_id, UserLlmTokenDaily.usage_date == day))
    if row is None:
        row = UserLlmTokenDaily(user_id=user_id, usage_date=day, consumed_tokens=0, updated_at=utcnow())
        db.add(row)
        db.flush()

    new_total = int(row.consumed_tokens) + tokens
    if new_total > limit:
        logger.warning(
            "LLM usage exceeds daily cap after call (user=%s day=%s %s+%s>%s); capping at limit.",
            user_id,
            day,
            row.consumed_tokens,
            tokens,
            limit,
        )
        new_total = limit
    row.consumed_tokens = new_total
    row.updated_at = utcnow()
    db.commit()


def today_consumed(db: Session, user_id: int) -> int:
    day = beijing_today_str()
    row = db.scalar(select(UserLlmTokenDaily).where(UserLlmTokenDaily.user_id == user_id, UserLlmTokenDaily.usage_date == day))
    return int(row.consumed_tokens) if row else 0


def usage_summary_for_user(db: Session, user_id: int) -> dict:
    user = db.get(User, user_id)
    if user is None:
        return {}
    default_lim = _get_or_create_platform_default(db)
    limit = effective_daily_token_limit(user, default_lim)
    used = today_consumed(db, user_id)
    return {
        "usage_date": beijing_today_str(),
        "consumed_today": used,
        "daily_limit": limit,
        "remaining": max(0, limit - used),
        "override": user.llm_daily_token_limit,
        "platform_default": default_lim,
    }


def admin_usage_rows(db: Session) -> list[dict]:
    default_lim = _get_or_create_platform_default(db)
    day = beijing_today_str()
    users = list(db.scalars(select(User).order_by(User.username.asc())).all())
    rows = []
    for u in users:
        row = db.scalar(select(UserLlmTokenDaily).where(UserLlmTokenDaily.user_id == u.id, UserLlmTokenDaily.usage_date == day))
        consumed = int(row.consumed_tokens) if row else 0
        lim = effective_daily_token_limit(u, default_lim)
        rows.append(
            {
                "user": u,
                "consumed_today": consumed,
                "daily_limit": lim,
                "override": u.llm_daily_token_limit,
            }
        )
    return rows


def admin_total_usage_all_time(db: Session) -> int:
    total = db.scalar(select(func.coalesce(func.sum(UserLlmTokenDaily.consumed_tokens), 0)))
    return int(total or 0)
