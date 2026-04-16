from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import (
    assign_user_role,
    can_verify_email_token,
    generate_internal_email_address,
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
    valid_registration_invite_code,
    verify_password,
)
from app.constants import AccountRole, PlatformRole, UserRole
from app.db import get_db
from app.i18n import set_locale, t
from app.models import User
from app.services.courses import bootstrap_sample_data
from app.services.email import send_verification_email
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
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url=landing_path_for_user(get_current_user(request, db)), status_code=303)
    return _render_register_form(request, db)


@router.get("/locale/{locale}")
def change_locale(locale: str, request: Request):
    set_locale(request, locale)
    redirect_to = request.headers.get("referer") or "/"
    return RedirectResponse(url=redirect_to, status_code=303)


@router.get("/register/pending")
def register_pending_page(
    request: Request,
    email: str = Query(default=""),
    db: Session = Depends(get_db),
):
    if get_current_user(request, db):
        return RedirectResponse(url=landing_path_for_user(get_current_user(request, db)), status_code=303)
    return _render_register_pending_page(request, db, email=email.strip())


@router.post("/register")
def register_user(
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
        db.commit()
        db.refresh(user)
        login_user(request, user)
        push_flash(request, t(request, "flash.invite_registration_success"), "success")
        return RedirectResponse(url=landing_path_for_user(user), status_code=303)

    user = User(
        username=username,
        email=normalized_email,
        password_hash=hash_password(password),
        account_role=AccountRole.STUDENT,
        platform_role=PlatformRole.USER,
        **initial_email_verification_state(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    delivery = send_verification_email(request, user)

    if delivery.delivered:
        push_flash(request, t(request, "flash.registration_pending_verification", email=user.email), "success")
    else:
        push_flash(request, t(request, "flash.verification_email_delivery_failed"), "warning")
    return RedirectResponse(url=f"/register/pending?email={user.email}", status_code=303)


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
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
def login(
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
    push_flash(request, t(request, "flash.login_success"), "success")
    return RedirectResponse(url=landing_path_for_user(user), status_code=303)


@router.get("/verify-email")
def verify_email(
    request: Request,
    token: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.email_verification_token == token))
    if user is None:
        push_flash(request, t(request, "flash.email_verification_invalid"), "danger")
        return RedirectResponse(url="/login", status_code=303)
    if user.email_verified:
        push_flash(request, t(request, "flash.email_already_verified"), "info")
        return RedirectResponse(url="/login", status_code=303)
    if not can_verify_email_token(user):
        refresh_email_verification(user)
        db.commit()
        db.refresh(user)
        send_verification_email(request, user)
        push_flash(request, t(request, "flash.email_verification_expired"), "warning")
        return RedirectResponse(url=f"/login?email={user.email}", status_code=303)

    mark_email_verified(user)
    if not has_super_admin(db, require_verified=True):
        assign_user_role(user, UserRole.SUPER_ADMIN)
    bootstrap_sample_data(db, user)
    db.commit()
    db.refresh(user)

    login_user(request, user)
    push_flash(request, t(request, "flash.email_verification_success"), "success")
    return RedirectResponse(url=landing_path_for_user(user), status_code=303)


@router.post("/verify-email/resend")
def resend_verification_email(
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

    refresh_email_verification(user)
    db.commit()
    db.refresh(user)
    delivery = send_verification_email(request, user)
    if delivery.delivered:
        push_flash(request, t(request, "flash.verification_email_resent"), "success")
    else:
        push_flash(request, t(request, "flash.verification_email_delivery_failed"), "warning")
    return RedirectResponse(url=f"/login?email={user.email}", status_code=303)


@router.post("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)
