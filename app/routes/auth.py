from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import (
    assign_user_role,
    can_verify_email_token,
    can_reset_password_token,
    can_send_email_verification,
    can_send_password_reset,
    clear_password_reset,
    find_user_by_email_verification_token,
    find_user_by_password_reset_token,
    generate_internal_email_address,
    generate_email_verification_token,
    initial_email_verification_state,
    find_user_by_login,
    get_current_user,
    has_super_admin,
    hash_password,
    invite_registration_enabled,
    landing_path_for_user,
    login_user,
    logout_user,
    mark_email_verified,
    push_flash,
    refresh_email_verification,
    refresh_password_reset,
    valid_registration_invite_code,
    verify_password,
)
from app.constants import AccountRole, PlatformRole, UserRole
from app.db import get_db, utcnow
from app.i18n import choose_text, set_locale, t
from app.models import User
from app.services.courses import bootstrap_sample_data, ensure_user_in_open_community_course
from app.services.email import send_password_reset_email, send_verification_email
from app.services.permissions import RedirectRequired, require_user
from app.services.redirects import safe_referer_redirect
from app.services.storage_paths import absolute_data_path
from app.services.upload_limits import read_upload_file_limited
from app.services.user_media import store_user_avatar
from app.services.llm_token_usage import usage_summary_for_user
from app.services.user_storage import (
    PROFILE_ASSETS_PAGE_SIZE,
    QuotaExceededError,
    describe_asset_for_profile,
    effective_storage_quota_bytes,
    list_user_assets_page,
    purge_user_asset,
    record_stored_object,
    remove_avatar_storage,
    total_used_bytes,
    viewer_may_purge_asset,
)
from app.web import render_template


router = APIRouter()


def _register_form_data(
    *,
    username: str = "",
    email: str = "",
    registration_mode: str = "email",
    invite_code: str = "",
) -> dict[str, str]:
    return {
        "username": username,
        "email": email,
        "registration_mode": registration_mode,
        "invite_code": invite_code,
    }


def _render_register_form(
    request: Request,
    db: Session,
    *,
    form_data: dict[str, str] | None = None,
    status_code: int = 200,
    register_error: bool = False,
):
    return render_template(
        request,
        db,
        "register.html",
        {
            "form_data": form_data or _register_form_data(),
            "register_error": register_error,
            "invite_registration_enabled": invite_registration_enabled(),
        },
        status_code=status_code,
    )


def _render_register_pending_page(
    request: Request,
    db: Session,
    *,
    email: str = "",
):
    return render_template(
        request,
        db,
        "register_pending.html",
        {
            "pending_email": email,
        },
    )


@router.get("/register")
async def register_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url=landing_path_for_user(get_current_user(request, db)), status_code=303)
    return _render_register_form(request, db)


@router.get("/locale/{locale}")
async def change_locale(locale: str, request: Request):
    set_locale(request, locale)
    redirect_to = safe_referer_redirect(
        request.headers.get("referer"),
        "/",
        request_host=(request.url.hostname or ""),
        request_port=request.url.port,
    )
    return RedirectResponse(url=redirect_to, status_code=303)


@router.get("/register/pending")
async def register_pending_page(
    request: Request,
    email: str = Query(default=""),
    db: Session = Depends(get_db),
):
    if get_current_user(request, db):
        return RedirectResponse(url=landing_path_for_user(get_current_user(request, db)), status_code=303)
    return _render_register_pending_page(request, db, email=email.strip())


@router.get("/forgot-password")
async def forgot_password_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url=landing_path_for_user(get_current_user(request, db)), status_code=303)
    return render_template(request, db, "forgot_password.html", {})


@router.post("/forgot-password")
async def forgot_password(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    try:
        normalized_email = validate_email(normalized_email, check_deliverability=False).normalized
    except EmailNotValidError:
        push_flash(request, t(request, "flash.password_reset_sent"), "success")
        return RedirectResponse(url="/login", status_code=303)

    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is not None and user.is_active and user.email_verified and can_send_password_reset(user):
        reset_token = refresh_password_reset(user)
        db.commit()
        db.refresh(user)
        send_password_reset_email(request, user, reset_token, db=db)

    push_flash(request, t(request, "flash.password_reset_sent"), "success")
    return RedirectResponse(url="/login", status_code=303)


@router.get("/reset-password")
async def reset_password_page(
    request: Request,
    token: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    user = find_user_by_password_reset_token(db, token)
    if user is None or not can_reset_password_token(user):
        push_flash(request, t(request, "flash.password_reset_invalid"), "danger")
        return RedirectResponse(url="/forgot-password", status_code=303)
    return render_template(request, db, "reset_password.html", {"reset_token": token})


@router.post("/reset-password")
async def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    token = token.strip()
    user = find_user_by_password_reset_token(db, token)
    if user is None or not can_reset_password_token(user):
        push_flash(request, t(request, "flash.password_reset_invalid"), "danger")
        return RedirectResponse(url="/forgot-password", status_code=303)
    if password != confirm_password:
        push_flash(request, t(request, "flash.password_mismatch"), "danger")
        return render_template(request, db, "reset_password.html", {"reset_token": token}, status_code=400)
    if len(password) < 8:
        push_flash(request, t(request, "flash.password_length"), "danger")
        return render_template(request, db, "reset_password.html", {"reset_token": token}, status_code=400)

    user.password_hash = hash_password(password)
    clear_password_reset(user)
    user.updated_at = utcnow()
    db.commit()
    push_flash(request, t(request, "flash.password_reset_success"), "success")
    return RedirectResponse(url=f"/login?email={user.email}", status_code=303)


@router.post("/register")
async def register_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    registration_mode: str = Form("email"),
    invite_code: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    email = email.strip().lower()
    registration_mode = registration_mode.strip().lower()
    invite_code = invite_code.strip()
    if registration_mode not in {"email", "invite"}:
        registration_mode = "email"
    form_data = _register_form_data(
        username=username,
        email=email,
        registration_mode=registration_mode,
        invite_code=invite_code,
    )

    if not username or not password or not confirm_password:
        push_flash(request, t(request, "flash.all_fields_required"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
    if password != confirm_password:
        push_flash(request, t(request, "flash.password_mismatch"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
    if len(password) < 8:
        push_flash(request, t(request, "flash.password_length"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)

    normalized_email = ""
    if email:
        try:
            normalized_email = validate_email(email, check_deliverability=False).normalized
        except EmailNotValidError:
            push_flash(request, t(request, "flash.invalid_email"), "danger")
            return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
    elif registration_mode == "email":
        push_flash(request, t(request, "flash.email_required_for_email_registration"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)

    if registration_mode == "invite":
        if not invite_registration_enabled():
            push_flash(request, t(request, "flash.invite_registration_disabled"), "danger")
            return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
        if not invite_code:
            push_flash(request, t(request, "flash.invite_code_required"), "danger")
            return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
        if not valid_registration_invite_code(invite_code):
            push_flash(request, t(request, "flash.invalid_invite_code"), "danger")
            return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)

    existing_user_query = select(User).where(User.username == username)
    if normalized_email:
        existing_user_query = select(User).where(or_(User.username == username, User.email == normalized_email))
    existing_user = db.scalar(existing_user_query)
    if existing_user:
        push_flash(request, t(request, "flash.username_email_exists"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)

    if registration_mode == "invite":
        invite_email = normalized_email
        while not invite_email:
            generated_email = generate_internal_email_address()
            if db.scalar(select(User.id).where(User.email == generated_email)) is None:
                invite_email = generated_email
        user = User(
            username=username,
            email=invite_email,
            password_hash=hash_password(password),
            account_role=AccountRole.STUDENT,
            platform_role=PlatformRole.USER,
            email_verified=True,
            email_verification_token=None,
            email_verification_sent_at=None,
        )
        if not has_super_admin(db, require_verified=True):
            assign_user_role(user, UserRole.SUPER_ADMIN)
        db.add(user)
        db.commit()
        db.refresh(user)
        bootstrap_sample_data(db, user)
        ensure_user_in_open_community_course(db, user)
        db.commit()
        db.refresh(user)
        login_user(request, user)
        push_flash(request, t(request, "flash.invite_registration_success"), "success")
        return RedirectResponse(url=landing_path_for_user(user), status_code=303)

    verification_token = generate_email_verification_token()
    user = User(
        username=username,
        email=normalized_email,
        password_hash=hash_password(password),
        account_role=AccountRole.STUDENT,
        platform_role=PlatformRole.USER,
        **initial_email_verification_state(verification_token),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    delivery = send_verification_email(request, user, verification_token, db=db)

    if delivery.delivered:
        push_flash(request, t(request, "flash.registration_pending_verification", email=user.email), "success")
    else:
        push_flash(request, t(request, "flash.verification_email_delivery_failed"), "warning")
    return RedirectResponse(url=f"/register/pending?email={user.email}", status_code=303)


@router.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url=landing_path_for_user(get_current_user(request, db)), status_code=303)
    email = request.query_params.get("email", "").strip()
    return render_template(
        request,
        db,
        "login.html",
        {
            "login_value": email,
            "resend_email": email,
        },
    )


@router.post("/login")
async def login(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = find_user_by_login(db, login.strip())
    if user is None or not verify_password(password, user.password_hash):
        push_flash(request, t(request, "flash.invalid_login"), "danger")
        return RedirectResponse(url="/login", status_code=303)
    if not user.email_verified:
        push_flash(request, t(request, "flash.email_verification_required"), "warning")
        return RedirectResponse(url=f"/login?email={user.email}", status_code=303)

    login_user(request, user)
    ensure_user_in_open_community_course(db, user)
    db.commit()
    push_flash(request, t(request, "flash.login_success"), "success")
    return RedirectResponse(url=landing_path_for_user(user), status_code=303)


@router.get("/verify-email")
async def verify_email(
    request: Request,
    token: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    user = find_user_by_email_verification_token(db, token)
    if user is None:
        push_flash(request, t(request, "flash.email_verification_invalid"), "danger")
        return RedirectResponse(url="/login", status_code=303)
    if user.email_verified:
        push_flash(request, t(request, "flash.email_already_verified"), "info")
        return RedirectResponse(url="/login", status_code=303)
    if not can_verify_email_token(user):
        new_token = refresh_email_verification(user)
        db.commit()
        db.refresh(user)
        send_verification_email(request, user, new_token, db=db)
        push_flash(request, t(request, "flash.email_verification_expired"), "warning")
        return RedirectResponse(url=f"/login?email={user.email}", status_code=303)

    mark_email_verified(user)
    if not has_super_admin(db, require_verified=True):
        assign_user_role(user, UserRole.SUPER_ADMIN)
    bootstrap_sample_data(db, user)
    ensure_user_in_open_community_course(db, user)
    db.commit()
    db.refresh(user)

    login_user(request, user)
    push_flash(request, t(request, "flash.email_verification_success"), "success")
    return RedirectResponse(url=landing_path_for_user(user), status_code=303)


@router.post("/verify-email/resend")
async def resend_verification_email(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    try:
        normalized_email = validate_email(normalized_email, check_deliverability=False).normalized
    except EmailNotValidError:
        push_flash(request, t(request, "flash.invalid_email"), "danger")
        return RedirectResponse(url="/login", status_code=303)

    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        push_flash(request, t(request, "flash.verification_email_resent"), "success")
        return RedirectResponse(url="/login", status_code=303)
    if user.email_verified:
        push_flash(request, t(request, "flash.email_already_verified"), "info")
        return RedirectResponse(url="/login", status_code=303)

    if not can_send_email_verification(user):
        push_flash(request, t(request, "flash.email_resend_rate_limited"), "warning")
        return RedirectResponse(url=f"/login?email={user.email}", status_code=303)

    verification_token = refresh_email_verification(user)
    db.commit()
    db.refresh(user)
    delivery = send_verification_email(request, user, verification_token, db=db)
    if delivery.delivered:
        push_flash(request, t(request, "flash.verification_email_resent"), "success")
    else:
        push_flash(request, t(request, "flash.verification_email_delivery_failed"), "warning")
    return RedirectResponse(url=f"/login?email={user.email}", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)


@router.get("/me/profile")
async def profile_page(
    request: Request,
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)
    rows, total, total_pages = list_user_assets_page(db, user.id, page=page)
    assets = []
    for row in rows:
        item = describe_asset_for_profile(db, row, user.id)
        assets.append(
            {
                "id": item.object_id,
                "label_en": item.label_en,
                "label_zh": item.label_zh,
                "link_url": item.link_url,
                "size_bytes": item.size_bytes,
                "can_delete": viewer_may_purge_asset(db, user, user.id, row),
            }
        )
    return render_template(
        request,
        db,
        "profile.html",
        {
            "profile_user": user,
            "profile_section": "overview",
            "storage_used_bytes": total_used_bytes(db, user.id),
            "storage_quota_bytes": effective_storage_quota_bytes(db, user),
            "profile_assets": assets,
            "profile_assets_page": page,
            "profile_assets_total_pages": total_pages,
            "profile_assets_total": total,
            "profile_assets_page_size": PROFILE_ASSETS_PAGE_SIZE,
        },
    )


@router.get("/me/profile/llm-usage")
async def profile_llm_usage(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)
    summary = usage_summary_for_user(db, user.id)
    return render_template(
        request,
        db,
        "profile_llm_usage.html",
        {"llm_usage": summary, "profile_section": "llm"},
    )


@router.post("/me/profile/assets/{object_id}/delete")
async def profile_delete_asset(object_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)
    err = purge_user_asset(db, viewer=user, target_user_id=user.id, object_id=object_id)
    if err == "not_found":
        push_flash(request, choose_text(request, "Asset not found.", "未找到该文件记录。"), "danger")
    elif err == "forbidden":
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
    elif err == "unsupported":
        push_flash(request, choose_text(request, "This asset cannot be removed here.", "无法在此处删除该资源。"), "warning")
    else:
        push_flash(request, choose_text(request, "Asset removed.", "已删除。"), "success")
    return RedirectResponse(url="/me/profile", status_code=303)


@router.post("/me/profile/avatar")
async def profile_upload_avatar(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)
    if user.avatar_banned:
        push_flash(request, t(request, "flash.avatar_banned"), "danger")
        return RedirectResponse(url="/me/profile", status_code=303)
    try:
        raw = await read_upload_file_limited(file)
        remove_avatar_storage(db, user)
        rel = store_user_avatar(user.id, raw, file.filename or "avatar.png")
        sz = absolute_data_path(rel).stat().st_size
        record_stored_object(
            db,
            user_id=user.id,
            category="avatar",
            relative_path=rel,
            size_bytes=sz,
            ref_type="user",
            ref_id=user.id,
        )
    except QuotaExceededError:
        push_flash(
            request,
            choose_text(request, "Storage quota exceeded.", "存储空间已满，无法上传。"),
            "danger",
        )
        return RedirectResponse(url="/me/profile", status_code=303)
    except ValueError as exc:
        msg = "unsupported_image_type" if str(exc) == "unsupported_image_type" else str(exc)
        push_flash(request, t(request, "flash.invalid_avatar") if msg == "unsupported_image_type" else msg, "danger")
        return RedirectResponse(url="/me/profile", status_code=303)
    user.avatar_path = rel
    user.avatar_banned = False
    user.updated_at = utcnow()
    db.commit()
    push_flash(request, t(request, "flash.avatar_updated"), "success")
    return RedirectResponse(url="/me/profile", status_code=303)


@router.post("/me/profile/avatar/remove")
async def profile_remove_avatar(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return RedirectResponse(url=redirect.location, status_code=303)
    remove_avatar_storage(db, user)
    user.avatar_path = None
    user.updated_at = utcnow()
    db.commit()
    push_flash(request, t(request, "flash.avatar_removed"), "success")
    return RedirectResponse(url="/me/profile", status_code=303)
