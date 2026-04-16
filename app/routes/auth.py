from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import (
    initial_email_verification_state,
    find_user_by_login,
    get_current_user,
    hash_password,
    login_user,
    logout_user,
    push_flash,
    resolve_registration_roles,
    verify_password,
)
from app.db import get_db
from app.i18n import set_locale, t
from app.models import User
from app.services.courses import bootstrap_sample_data
from app.web import render_template


router = APIRouter()


def _register_form_data(
    *,
    username: str = "",
    email: str = "",
) -> dict[str, str]:
    return {
        "username": username,
        "email": email,
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
        },
        status_code=status_code,
    )


@router.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/dashboard", status_code=303)
    return _render_register_form(request, db)


@router.get("/locale/{locale}")
def change_locale(locale: str, request: Request):
    set_locale(request, locale)
    redirect_to = request.headers.get("referer") or "/"
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/register")
def register_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    email = email.strip().lower()
    form_data = _register_form_data(
        username=username,
        email=email,
    )

    if not username or not email or not password or not confirm_password:
        push_flash(request, t(request, "flash.all_fields_required"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
    try:
        email = validate_email(email, check_deliverability=False).normalized
    except EmailNotValidError:
        push_flash(request, t(request, "flash.invalid_email"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
    if password != confirm_password:
        push_flash(request, t(request, "flash.password_mismatch"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)
    if len(password) < 8:
        push_flash(request, t(request, "flash.password_length"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)

    existing_user = db.scalar(select(User).where(or_(User.username == username, User.email == email)))
    if existing_user:
        push_flash(request, t(request, "flash.username_email_exists"), "danger")
        return _render_register_form(request, db, form_data=form_data, status_code=400, register_error=True)

    account_role, platform_role = resolve_registration_roles(db)
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        account_role=account_role,
        platform_role=platform_role,
        **initial_email_verification_state(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    bootstrap_sample_data(db, user)

    login_user(request, user)
    push_flash(request, t(request, "flash.registration_success"), "success")
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/dashboard", status_code=303)
    return render_template(request, db, "login.html")


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

    login_user(request, user)
    push_flash(request, t(request, "flash.login_success"), "success")
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)
