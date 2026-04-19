from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.constants import LLMScope, LLMTestStatus
from app.models import LLMConfig, LLMConfigMember
from app.services.llm_retry import retry_llm_grading_call

LLMTarget = LLMConfig | LLMConfigMember
T = TypeVar("T")


def group_description(group: LLMConfig) -> str:
    explicit = (group.description or "").strip()
    if explicit:
        return explicit
    return _target_description(group)


def _target_description(target: LLMTarget) -> str:
    model = (target.model_name or "").strip()
    url = (target.base_url or "").strip()
    if model and url:
        return f"{model} @ {url}"
    return model or url or "-"


def target_display_name(target: LLMTarget) -> str:
    if isinstance(target, LLMConfig):
        return f"#1 {_target_description(target)}"
    return f"#{target.priority_order} {_target_description(target)}"


def ordered_group_targets(group: LLMConfig, *, tested_only: bool = True) -> list[LLMTarget]:
    targets: list[LLMTarget] = []
    if _target_available(group, tested_only=tested_only):
        targets.append(group)
    for member in sorted(group.members, key=lambda item: (item.priority_order, item.id or 0)):
        if _target_available(member, tested_only=tested_only):
            targets.append(member)
    return targets


def _target_available(target: LLMTarget, *, tested_only: bool) -> bool:
    if not target.enabled:
        return False
    if tested_only and target.last_test_status != LLMTestStatus.SUCCESS:
        return False
    return True


def group_has_callable_target(group: LLMConfig) -> bool:
    return group.enabled and bool(ordered_group_targets(group, tested_only=True))


def group_has_any_enabled_target(group: LLMConfig) -> bool:
    """Enabled group with at least one enabled primary or member (connectivity test may still be pending)."""
    return group.enabled and bool(ordered_group_targets(group, tested_only=False))


def first_group_with_any_enabled_target(db: Session, group_id: int | None) -> LLMConfig | None:
    if not group_id:
        return None
    group = db.scalar(select(LLMConfig).options(selectinload(LLMConfig.members)).where(LLMConfig.id == group_id))
    if group is None or not group_has_any_enabled_target(group):
        return None
    return group


def latest_platform_llm_group(db: Session) -> LLMConfig | None:
    groups = list(
        db.scalars(
            select(LLMConfig)
            .options(selectinload(LLMConfig.members))
            .where(LLMConfig.scope == LLMScope.PLATFORM, LLMConfig.enabled.is_(True))
            .order_by(LLMConfig.last_tested_at.desc(), LLMConfig.created_at.desc())
        ).all()
    )
    for group in groups:
        if group_has_callable_target(group):
            return group
    return None


def latest_platform_llm_group_relaxed(db: Session) -> LLMConfig | None:
    """Prefer recently tested platform groups, but allow groups whose endpoints are enabled yet not marked tested."""
    groups = list(
        db.scalars(
            select(LLMConfig)
            .options(selectinload(LLMConfig.members))
            .where(LLMConfig.scope == LLMScope.PLATFORM, LLMConfig.enabled.is_(True))
            .order_by(LLMConfig.last_tested_at.desc(), LLMConfig.created_at.desc())
        ).all()
    )
    for group in groups:
        if group_has_any_enabled_target(group):
            return group
    return None


def parse_optional_tested_llm_group_id(db: Session, raw: str, *, course_id: int | None = None) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        group_id = int(s)
    except ValueError:
        return None
    group = db.scalar(select(LLMConfig).options(selectinload(LLMConfig.members)).where(LLMConfig.id == group_id))
    if group is None or not group_has_callable_target(group):
        return None
    if course_id is not None and group.scope != LLMScope.PLATFORM and group.course_id != course_id:
        return None
    return group_id


def first_valid_group(db: Session, group_id: int | None) -> LLMConfig | None:
    if not group_id:
        return None
    group = db.scalar(select(LLMConfig).options(selectinload(LLMConfig.members)).where(LLMConfig.id == group_id))
    if group is None or not group_has_callable_target(group):
        return None
    return group


def available_llm_groups_for_course(db: Session, course_id: int | None = None) -> list[dict[str, object]]:
    filters = [LLMConfig.enabled.is_(True)]
    if course_id is None:
        filters.append(LLMConfig.scope == LLMScope.PLATFORM)
    else:
        filters.append((LLMConfig.scope == LLMScope.PLATFORM) | (LLMConfig.course_id == course_id))
    groups = list(
        db.scalars(
            select(LLMConfig)
            .options(selectinload(LLMConfig.members))
            .where(*filters)
            .order_by(LLMConfig.created_at.desc(), LLMConfig.id.desc())
        ).all()
    )
    return [
        {
            "id": group.id,
            "name": group.name,
            "description": group_description(group),
        }
        for group in groups
        if group_has_callable_target(group)
    ]


def call_llm_group(group: LLMConfig, fn: Callable[[LLMTarget], T], *, label: str) -> T:
    targets = ordered_group_targets(group, tested_only=True)
    if not targets:
        raise ValueError("No tested LLM is available in this group.")
    last_exc: Exception | None = None
    for target in targets:
        try:
            return retry_llm_grading_call(
                target,
                lambda target=target: fn(target),
                label=f"{label}:{target_display_name(target)}",
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise ValueError(f"All LLMs in group '{group.name}' failed.") from last_exc
