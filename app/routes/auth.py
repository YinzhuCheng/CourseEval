from fastapi import APIRouter, Depends, Form
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import (
    find_user_by_login,
    get_current_user,
    hash_password,
    login_user,
    logout_user,
    push_flash,
    verify_password,
)
from app.db import get_db
from app.models import User
from app.web import render_template


router = APIRouter()


@router.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/dashboard", status_code=303)
    return render_template(request, db, "register.html")


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

    if not username or not email or not password:
        push_flash(request, "All fields are required.", "danger")
        return RedirectResponse(url="/register", status_code=303)
    if password != confirm_password:
        push_flash(request, "Password confirmation does not match.", "danger")
        return RedirectResponse(url="/register", status_code=303)
    if len(password) < 8:
        push_flash(request, "Password must be at least 8 characters long.", "danger")
        return RedirectResponse(url="/register", status_code=303)

    existing_user = db.scalar(select(User).where(or_(User.username == username, User.email == email)))
    if existing_user:
        push_flash(request, "Username or email is already registered.", "danger")
        return RedirectResponse(url="/register", status_code=303)

    user = User(username=username, email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)

    login_user(request, user)
    push_flash(request, "Registration successful. Welcome!", "success")
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
        push_flash(request, "Invalid username/email or password.", "danger")
        return RedirectResponse(url="/login", status_code=303)

    login_user(request, user)
    push_flash(request, "Signed in successfully.", "success")
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)
