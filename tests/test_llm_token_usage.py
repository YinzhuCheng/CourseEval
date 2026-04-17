from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.constants import LLMProvider, LLMScope
from app.db import Base, utcnow
from app.models import LLMConfig, PlatformLlmTokenPolicy, User, UserLlmTokenDaily
from app.services.llm_token_usage import (
    assert_room_for_llm_call,
    beijing_today_str,
    effective_daily_token_limit,
    estimate_llm_call_budget,
    extract_total_tokens_from_response,
    record_llm_usage,
)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    session.add(PlatformLlmTokenPolicy(id=1, default_user_daily_llm_tokens=1000))
    session.add(User(id=1, username="u1", email="u1@example.com", password_hash="x"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def test_extract_openai_usage():
    raw = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    assert extract_total_tokens_from_response(LLMProvider.OPENAI_COMPATIBLE, raw) == 15


def test_extract_gemini_usage():
    raw = {"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3, "totalTokenCount": 10}}
    assert extract_total_tokens_from_response(LLMProvider.GEMINI, raw) == 10


def test_effective_limit_override(db_session: Session):
    u = db_session.get(User, 1)
    assert u is not None
    u.llm_daily_token_limit = 2000
    assert effective_daily_token_limit(u, 1000) == 2000
    u.llm_daily_token_limit = None
    assert effective_daily_token_limit(u, 1000) == 1000


def test_estimate_budget_scales_with_text_not_max_tokens_multiplier():
    cfg = LLMConfig(
        scope=LLMScope.PLATFORM,
        name="t",
        provider_type=LLMProvider.OPENAI_COMPATIBLE,
        model_name="m",
        max_tokens=100_000,
    )
    small = estimate_llm_call_budget(
        system_prompt="x" * 100,
        user_prompt="y" * 100,
        max_output_tokens_cap=int(cfg.max_tokens),
    )
    large = estimate_llm_call_budget(
        system_prompt="a" * 5000,
        user_prompt="b" * 5000,
        max_output_tokens_cap=int(cfg.max_tokens),
    )
    assert small < large
    assert small < 50_000


def test_estimate_multimodal_adds_per_image():
    text_only = estimate_llm_call_budget(
        system_prompt="s",
        user_prompt="p" * 2000,
        image_count=0,
        multimodal=False,
        max_output_tokens_cap=2048,
    )
    with_images = estimate_llm_call_budget(
        system_prompt="s",
        user_prompt="p" * 2000,
        image_count=3,
        multimodal=True,
        image_bytes_total=300_000,
        max_output_tokens_cap=2048,
    )
    assert with_images > text_only + 2000


def test_assert_room_blocks_on_estimated_budget_exceeding_remaining(db_session: Session):
    cfg = LLMConfig(
        scope=LLMScope.PLATFORM,
        name="t",
        provider_type=LLMProvider.OPENAI_COMPATIBLE,
        model_name="m",
        max_tokens=512,
    )
    db_session.add(
        UserLlmTokenDaily(user_id=1, usage_date=beijing_today_str(), consumed_tokens=500, updated_at=utcnow())
    )
    db_session.commit()
    with pytest.raises(ValueError, match="Estimated token budget"):
        assert_room_for_llm_call(db_session, 1, cfg, estimated_budget=600)


def test_record_caps_at_limit(db_session: Session):
    cfg = LLMConfig(
        scope=LLMScope.PLATFORM,
        name="t",
        provider_type=LLMProvider.OPENAI_COMPATIBLE,
        model_name="m",
    )
    day = beijing_today_str()
    db_session.add(UserLlmTokenDaily(user_id=1, usage_date=day, consumed_tokens=900, updated_at=utcnow()))
    db_session.commit()
    record_llm_usage(db_session, 1, cfg, {"usage": {"total_tokens": 500}})
    r = db_session.scalar(select(UserLlmTokenDaily).where(UserLlmTokenDaily.user_id == 1, UserLlmTokenDaily.usage_date == day))
    assert r is not None
    assert r.consumed_tokens == 1000
