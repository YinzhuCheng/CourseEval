"""Per-user daily LLM token limits and usage (calendar day in Asia/Shanghai)."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import LLMProvider
from app.db import utcnow
from app.models import LLMConfig, PlatformLlmTokenPolicy, User, UserLlmTokenDaily

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


def _estimated_upper_bound_per_call(config: LLMConfig) -> int:
    mt = int(config.max_tokens or 512)
    return min(500_000, max(8_000, mt * 4 + 8_000))


def assert_room_for_llm_call(db: Session, user_id: int, config: LLMConfig) -> tuple[int, str, int]:
    """Returns (limit, usage_date, consumed_before). Raises ValueError if quota exceeded."""
    platform_default = _get_or_create_platform_default(db)
    user = db.get(User, user_id)
    if user is None:
        raise ValueError("User not found for LLM token accounting.")
    limit = effective_daily_token_limit(user, platform_default)
    day = beijing_today_str()
    row = db.scalar(select(UserLlmTokenDaily).where(UserLlmTokenDaily.user_id == user_id, UserLlmTokenDaily.usage_date == day))
    consumed = int(row.consumed_tokens) if row else 0
    reserve = _estimated_upper_bound_per_call(config)
    if consumed >= limit:
        raise ValueError(
            f"Daily LLM token quota exceeded ({consumed}/{limit} for Beijing date {day}). "
            "Try again tomorrow or ask an administrator to raise your limit."
        )
    if consumed + reserve > limit:
        raise ValueError(
            f"Daily LLM token quota insufficient for this request (used {consumed}/{limit}, "
            f"need up to ~{reserve} tokens for Beijing date {day}). "
            "Try again tomorrow or ask an administrator to raise your limit."
        )
    return limit, day, consumed


def record_llm_usage(db: Session, user_id: int, config: LLMConfig, raw_response: dict | None) -> None:
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
