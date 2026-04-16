import json

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
    RuntimeScope,
    UserRole,
)
from app.db import get_db, utcnow
from app.models import LLMConfig, RuntimeImage, User
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
    return render_template(request, db, "admin_llm_configs.html", {"configs": configs})


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
