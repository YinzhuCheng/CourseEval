import json
from datetime import datetime

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import push_flash
from app.constants import (
    LLMProvider,
    LLMScope,
    LLMTestStatus,
    MembershipStatus,
    RuntimeScope,
    UserRole,
)
from app.db import get_db, utcnow
from app.models import (
    Course,
    CourseMember,
    DiscussionPost,
    DiscussionTopic,
    LLMConfig,
    PlatformLlmTokenPolicy,
    RuntimeImage,
    User,
)
from app.auth import assign_user_role
from app.i18n import choose_text, t
from app.runtime_support import (
    SUPPORTED_PYTHON_PACKAGES,
    SUPPORTED_PYTHON_VERSION,
    UNSUPPORTED_PACKAGE_NOTE_EN,
    UNSUPPORTED_PACKAGE_NOTE_ZH,
    default_runtime_package_summary,
)
from app.services.llm import test_llm_connectivity
from app.services.llm_token_usage import (
    admin_total_usage_all_time,
    admin_usage_rows,
    beijing_today_str,
    get_platform_default_daily_limit,
)
from app.services.discussion_ai import parse_optional_tested_llm_config_id
from app.services.discussions import hard_delete_post, mute_user_in_course, unmute_user_in_course
from app.services.email import send_smtp_test_email
from app.services.permissions import RedirectRequired, require_admin, require_super_admin
from app.web import render_template


router = APIRouter(prefix="/admin", tags=["admin"])


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(url=location, status_code=303)


@router.get("")
def admin_home(request: Request, db: Session = Depends(get_db)):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")
    return _redirect("/admin/system")


@router.get("/users")
def admin_users(request: Request, db: Session = Depends(get_db)):
    try:
        require_super_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    users = list(db.scalars(select(User).order_by(User.created_at.desc())).all())
    return render_template(request, db, "admin_users.html", {"users": users})


@router.post("/users/{user_id}/avatar/ban")
def admin_ban_user_avatar(user_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")
    target = db.get(User, user_id)
    if target is None:
        push_flash(request, t(request, "flash.user_not_found"), "danger")
        return _redirect("/admin/users")
    target.avatar_banned = True
    target.avatar_path = None
    target.updated_at = utcnow()
    db.commit()
    push_flash(request, choose_text(request, "Avatar banned for this user.", "已禁止该用户使用头像。"), "success")
    return _redirect("/admin/users")


@router.post("/users/{user_id}/avatar/unban")
def admin_unban_user_avatar(user_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")
    target = db.get(User, user_id)
    if target is None:
        push_flash(request, t(request, "flash.user_not_found"), "danger")
        return _redirect("/admin/users")
    target.avatar_banned = False
    target.updated_at = utcnow()
    db.commit()
    push_flash(request, choose_text(request, "Avatar ban lifted.", "已解除头像限制。"), "success")
    return _redirect("/admin/users")


@router.post("/users/{user_id}/role")
def admin_update_user_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        current_user = require_super_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    user = db.get(User, user_id)
    if user is None:
        push_flash(request, t(request, "flash.user_not_found"), "danger")
        return _redirect("/admin/users")

    if user.id == current_user.id:
        push_flash(request, t(request, "flash.cannot_change_own_role"), "warning")
        return _redirect("/admin/users")
    if user.effective_role == UserRole.SUPER_ADMIN:
        push_flash(request, t(request, "flash.super_admin_role_locked"), "warning")
        return _redirect("/admin/users")

    try:
        target_role = UserRole(role)
    except ValueError:
        push_flash(request, t(request, "flash.invalid_role_selection"), "danger")
        return _redirect("/admin/users")

    if target_role == UserRole.SUPER_ADMIN:
        push_flash(request, t(request, "flash.super_admin_role_locked"), "warning")
        return _redirect("/admin/users")

    assign_user_role(user, target_role)
    user.updated_at = utcnow()
    db.commit()
    push_flash(request, t(request, "flash.user_role_updated"), "success")
    return _redirect("/admin/users")


@router.get("/runtime-images")
def admin_runtime_images(request: Request, db: Session = Depends(get_db)):
    try:
        admin_user = require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    images = list(db.scalars(select(RuntimeImage).order_by(RuntimeImage.created_at.desc())).all())
    return render_template(
        request,
        db,
        "admin_runtime_images.html",
        {
            "images": images,
            "default_python_version": SUPPORTED_PYTHON_VERSION,
            "default_python_packages": SUPPORTED_PYTHON_PACKAGES,
            "default_runtime_package_summary": default_runtime_package_summary(),
            "default_package_note": choose_text(
                request,
                UNSUPPORTED_PACKAGE_NOTE_EN,
                UNSUPPORTED_PACKAGE_NOTE_ZH,
            ),
        },
    )


@router.post("/runtime-images")
def admin_create_runtime_image(
    request: Request,
    name: str = Form(...),
    image_tag: str = Form(...),
    python_version: str = Form(SUPPORTED_PYTHON_VERSION),
    package_summary: str = Form(""),
    network_enabled: str = Form("false"),
    timeout_seconds: int = Form(300),
    memory_limit_mb: int = Form(1024),
    cpu_limit: str = Form("1"),
    db: Session = Depends(get_db),
):
    try:
        admin_user = require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    image = RuntimeImage(
        scope=RuntimeScope.PLATFORM,
        name=name.strip(),
        image_tag=image_tag.strip(),
        python_version=python_version.strip() or SUPPORTED_PYTHON_VERSION,
        package_summary=package_summary.strip() or default_runtime_package_summary(),
        network_enabled=network_enabled == "true",
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
        cpu_limit=cpu_limit.strip() or "1",
        created_by=admin_user.id,
    )
    db.add(image)
    db.commit()
    push_flash(request, t(request, "flash.runtime_image_created"), "success")
    return _redirect("/admin/runtime-images")


@router.get("/llm-configs")
def admin_llm_configs(request: Request, db: Session = Depends(get_db)):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    configs = list(db.scalars(select(LLMConfig).order_by(LLMConfig.created_at.desc())).all())
    platform_default = get_platform_default_daily_limit(db)
    policy = db.get(PlatformLlmTokenPolicy, 1)
    dpp = int(policy.discussion_posts_page_size) if policy else 50
    return render_template(
        request,
        db,
        "admin_llm_configs.html",
        {
            "configs": configs,
            "platform_default_daily_tokens": platform_default,
            "beijing_usage_date": beijing_today_str(),
            "token_usage_rows": admin_usage_rows(db),
            "total_llm_tokens_recorded": admin_total_usage_all_time(db),
            "discussion_ai_policy": policy,
            "discussion_posts_page_size": max(10, min(200, dpp)),
        },
    )


@router.post("/llm-configs/discussion-ai")
def admin_discussion_ai_llm_overrides(
    request: Request,
    discussion_default_llm_config_id: str = Form(""),
    discussion_question_llm_config_id: str = Form(""),
    discussion_material_llm_config_id: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    row = db.get(PlatformLlmTokenPolicy, 1)
    if row is None:
        row = PlatformLlmTokenPolicy(id=1, default_user_daily_llm_tokens=100000)
        db.add(row)
        db.flush()

    d0 = parse_optional_tested_llm_config_id(db, discussion_default_llm_config_id)
    dq = parse_optional_tested_llm_config_id(db, discussion_question_llm_config_id)
    dm = parse_optional_tested_llm_config_id(db, discussion_material_llm_config_id)
    if discussion_default_llm_config_id.strip() and d0 is None:
        push_flash(request, choose_text(request, "Invalid default discussion AI config.", "讨论区 AI 默认模型无效或未通过测试。"), "danger")
        return _redirect("/admin/llm-configs")
    if discussion_question_llm_config_id.strip() and dq is None:
        push_flash(request, choose_text(request, "Invalid question-discussion AI config.", "习题讨论 AI 模型无效或未通过测试。"), "danger")
        return _redirect("/admin/llm-configs")
    if discussion_material_llm_config_id.strip() and dm is None:
        push_flash(request, choose_text(request, "Invalid material-discussion AI config.", "资料讨论 AI 模型无效或未通过测试。"), "danger")
        return _redirect("/admin/llm-configs")

    row.discussion_ai_default_llm_config_id = d0
    row.discussion_ai_question_llm_config_id = dq
    row.discussion_ai_material_llm_config_id = dm
    row.updated_at = utcnow()
    db.commit()
    push_flash(request, choose_text(request, "Discussion AI defaults were saved.", "讨论区 AI 默认配置已保存。"), "success")
    return _redirect("/admin/llm-configs")


@router.post("/llm-configs/discussion-pagination")
def admin_update_discussion_pagination(
    request: Request,
    discussion_posts_page_size: int = Form(50),
    db: Session = Depends(get_db),
):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    value = max(10, min(200, int(discussion_posts_page_size)))
    row = db.get(PlatformLlmTokenPolicy, 1)
    if row is None:
        row = PlatformLlmTokenPolicy(id=1, default_user_daily_llm_tokens=100000, discussion_posts_page_size=value)
        db.add(row)
    else:
        row.discussion_posts_page_size = value
        row.updated_at = utcnow()
    db.commit()
    push_flash(
        request,
        choose_text(request, "Discussion page size updated.", "讨论区分页大小已更新。"),
        "success",
    )
    return _redirect("/admin/llm-configs")


@router.post("/llm-configs/token-policy")
def admin_update_llm_token_policy(
    request: Request,
    default_user_daily_llm_tokens: int = Form(100000),
    db: Session = Depends(get_db),
):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    value = max(1_000, min(500_000_000, int(default_user_daily_llm_tokens)))
    row = db.get(PlatformLlmTokenPolicy, 1)
    if row is None:
        row = PlatformLlmTokenPolicy(id=1, default_user_daily_llm_tokens=value)
        db.add(row)
    else:
        row.default_user_daily_llm_tokens = value
        row.updated_at = utcnow()
    db.commit()
    push_flash(request, t(request, "flash.llm_token_policy_updated"), "success")
    return _redirect("/admin/llm-configs")


@router.post("/llm-configs/user-token-limit")
def admin_update_user_llm_token_limit(
    request: Request,
    user_id: int = Form(...),
    daily_limit: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    user = db.get(User, user_id)
    if user is None:
        push_flash(request, t(request, "flash.user_not_found"), "danger")
        return _redirect("/admin/llm-configs")
    raw = (daily_limit or "").strip()
    if raw == "":
        user.llm_daily_token_limit = None
    else:
        try:
            lim = int(raw)
        except ValueError:
            push_flash(request, t(request, "flash.llm_token_limit_invalid"), "danger")
            return _redirect("/admin/llm-configs")
        user.llm_daily_token_limit = max(1_000, min(500_000_000, lim))
    user.updated_at = utcnow()
    db.commit()
    push_flash(request, t(request, "flash.llm_user_token_limit_updated"), "success")
    return _redirect("/admin/llm-configs")


@router.post("/llm-configs")
def admin_create_llm_config(
    request: Request,
    name: str = Form(...),
    provider_type: str = Form(...),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model_name: str = Form(...),
    timeout_seconds: int = Form(30),
    max_tokens: int = Form(512),
    temperature: str = Form("0.2"),
    queue_concurrency: int = Form(1),
    max_llm_retries: int = Form(3),
    llm_retry_initial_seconds: int = Form(5),
    db: Session = Depends(get_db),
):
    try:
        admin_user = require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    config = LLMConfig(
        scope=LLMScope.PLATFORM,
        name=name.strip(),
        provider_type=LLMProvider(provider_type),
        base_url=base_url.strip() or None,
        api_key=api_key.strip() or None,
        model_name=model_name.strip(),
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        temperature=temperature.strip(),
        queue_concurrency=max(queue_concurrency, 1),
        max_llm_retries=max(1, max_llm_retries),
        llm_retry_initial_seconds=max(1, llm_retry_initial_seconds),
        created_by=admin_user.id,
    )
    db.add(config)
    db.commit()
    push_flash(request, t(request, "flash.llm_config_created"), "success")
    return _redirect("/admin/llm-configs")


@router.post("/llm-configs/{config_id}/test")
def admin_test_llm_config(config_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    config = db.get(LLMConfig, config_id)
    if config is None:
        push_flash(request, t(request, "flash.llm_config_not_found"), "danger")
        return _redirect("/admin/llm-configs")

    result = test_llm_connectivity(config)
    config.last_test_status = LLMTestStatus.SUCCESS if result.success else LLMTestStatus.FAILED
    config.last_test_message = result.message
    flash_category = "success" if result.success else "danger"
    flash_message = (
        t(request, "flash.llm_test_success")
        if result.success
        else t(request, "flash.llm_test_failed", message=result.message)
    )
    config.last_tested_at = utcnow()
    db.commit()
    push_flash(request, flash_message, flash_category)
    return _redirect("/admin/llm-configs")


@router.post("/system/smtp-test")
def admin_test_smtp(
    request: Request,
    recipient: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        admin_user = require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    try:
        normalized_recipient = validate_email(recipient.strip().lower(), check_deliverability=False).normalized
    except EmailNotValidError:
        push_flash(request, t(request, "flash.invalid_email"), "danger")
        return _redirect("/admin/system")

    result = send_smtp_test_email(request, normalized_recipient, db=db, user=admin_user)
    if result.delivered:
        push_flash(request, t(request, "flash.smtp_test_success"), "success")
    else:
        push_flash(request, t(request, "flash.smtp_test_failed", message=result.error_message), "danger")
    return _redirect("/admin/system")


@router.get("/system")
def admin_system(request: Request, db: Session = Depends(get_db)):
    try:
        require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")

    users = list(db.scalars(select(User)).all())
    stats = {
        "users": len(users),
        "super_admins": len([user for user in users if user.effective_role == UserRole.SUPER_ADMIN]),
        "admins": len([user for user in users if user.effective_role == UserRole.ADMIN]),
        "teachers": len([user for user in users if user.effective_role == UserRole.TEACHER]),
        "students": len([user for user in users if user.effective_role == UserRole.STUDENT]),
        "runtime_images": len(list(db.scalars(select(RuntimeImage)).all())),
        "llm_configs": len(list(db.scalars(select(LLMConfig)).all())),
    }
    return render_template(
        request,
        db,
        "admin_system.html",
        {"stats": stats, "raw_stats_json": json.dumps(stats, indent=2)},
    )


@router.post("/discussion/delete-post")
def admin_delete_discussion_post(
    request: Request,
    post_id: int = Form(...),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        admin_user = require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")
    post = db.get(DiscussionPost, post_id)
    if post is None or getattr(post, "deleted_at", None) is not None:
        push_flash(request, choose_text(request, "Post not found.", "未找到帖子。"), "danger")
        return _redirect(redirect_to or "/admin/system")
    topic = db.get(DiscussionTopic, post.topic_id)
    if topic is None:
        return _redirect(redirect_to or "/admin/system")
    hard_delete_post(db, post, actor=admin_user, course_id=topic.course_id)
    db.commit()
    push_flash(request, choose_text(request, "Post deleted.", "帖子已删除。"), "success")
    return _redirect(redirect_to or "/admin/system")


@router.post("/courses/{course_id}/discussion/mute-user")
def admin_mute_discussion_user(
    course_id: int,
    request: Request,
    user_id: int = Form(...),
    muted_until: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        admin_user = require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")
    course = db.get(Course, course_id)
    if course is None:
        push_flash(request, choose_text(request, "Course not found.", "课程不存在。"), "danger")
        return _redirect(redirect_to or "/admin/users")
    target = db.get(User, user_id)
    if target is None:
        push_flash(request, choose_text(request, "User not found.", "用户不存在。"), "danger")
        return _redirect(redirect_to or "/admin/users")
    member = db.scalar(
        select(CourseMember).where(
            CourseMember.course_id == course_id,
            CourseMember.user_id == user_id,
            CourseMember.status == MembershipStatus.ACTIVE,
        )
    )
    if member is None:
        push_flash(
            request,
            choose_text(request, "User is not an active member of this course.", "该用户不是此课程的活跃成员。"),
            "danger",
        )
        return _redirect(redirect_to or "/admin/users")
    until_dt = None
    raw = (muted_until or "").strip()
    if raw:
        try:
            until_dt = datetime.fromisoformat(raw)
            if until_dt.tzinfo is None:
                from datetime import timezone

                until_dt = until_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            push_flash(request, choose_text(request, "Invalid end time.", "结束时间无效。"), "danger")
            return _redirect(redirect_to or "/admin/users")
    mute_user_in_course(db, course_id=course_id, target_user_id=target.id, actor=admin_user, muted_until=until_dt)
    db.commit()
    push_flash(
        request,
        choose_text(request, "User muted from course discussions.", "已禁止该用户在此课程讨论区发言。"),
        "success",
    )
    return _redirect(redirect_to or "/admin/users")


@router.post("/courses/{course_id}/discussion/unmute-user")
def admin_unmute_discussion_user(
    course_id: int,
    request: Request,
    user_id: int = Form(...),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        admin_user = require_admin(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except PermissionError:
        return _redirect("/login")
    unmute_user_in_course(db, course_id=course_id, target_user_id=user_id, actor=admin_user)
    db.commit()
    push_flash(request, choose_text(request, "Mute removed.", "已解除禁言。"), "success")
    return _redirect(redirect_to or "/admin/users")
