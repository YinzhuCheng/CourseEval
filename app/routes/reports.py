"""Report submission and review workflows."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import is_admin, push_flash
from app.constants import CourseRole, MembershipStatus, ReportTargetType
from app.db import get_db
from app.i18n import choose_text
from app.models import CourseMember, Report
from app.services.permissions import RedirectRequired, require_user
from app.services.redirects import safe_local_redirect
from app.services.reports import (
    REPORT_REASONS,
    can_handle_report,
    create_report,
    list_reports_by_user,
    list_reports_for_reviewer,
    update_report_status,
)
from app.web import render_template

router = APIRouter(tags=["reports"])


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


async def _extract_report_attachments(request: Request) -> list[tuple[bytes, str, str | None]]:
    form = await request.form()
    out: list[tuple[bytes, str, str | None]] = []
    for key in form:
        if not str(key).startswith("evidence"):
            continue
        f = form[key]
        if hasattr(f, "read"):
            content = await f.read()  # type: ignore[union-attr]
            if content:
                out.append((content, getattr(f, "filename", None) or "attachment.bin", getattr(f, "content_type", None)))
    return out


@router.get("/reports/new")
def new_report(
    request: Request,
    target_type: str,
    target_id: int,
    redirect_to: str = "",
    db: Session = Depends(get_db),
):
    try:
        require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    try:
        target = ReportTargetType(target_type)
    except ValueError:
        push_flash(request, choose_text(request, "Invalid report target.", "举报对象无效。"), "danger")
        return _redirect(redirect_to or "/student/courses")
    return render_template(
        request,
        db,
        "report_form.html",
        {
            "target_type": target.value,
            "target_id": target_id,
            "redirect_to": redirect_to,
            "report_reasons": REPORT_REASONS,
        },
    )


@router.post("/reports")
async def submit_report(
    request: Request,
    target_type: str = Form(...),
    target_id: int = Form(...),
    reason_code: str = Form(...),
    reason_text: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    attachments = await _extract_report_attachments(request)
    try:
        create_report(
            db,
            reporter=user,
            target_type=target_type,
            target_id=target_id,
            reason_code=reason_code,
            reason_text=reason_text,
            attachments=attachments,
        )
        db.commit()
    except PermissionError:
        db.rollback()
        push_flash(request, choose_text(request, "You cannot report this target.", "你不能举报该对象。"), "danger")
        return _redirect(redirect_to or "/student/courses")
    except ValueError as exc:
        db.rollback()
        msg = "Report failed." if str(exc) != "reason_required" else "Please provide a reason."
        zh = "举报失败。" if str(exc) != "reason_required" else "请填写举报理由。"
        push_flash(request, choose_text(request, msg, zh), "danger")
        return _redirect(redirect_to or "/student/courses")
    push_flash(request, choose_text(request, "Report submitted.", "举报已提交。"), "success")
    return _redirect(safe_local_redirect(redirect_to, "/student/courses"))


@router.get("/me/reports")
def my_reports(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(request, db, "my_reports.html", {"reports": list_reports_by_user(db, user)})


@router.get("/admin/reports")
def admin_reports(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    course_staff = db.scalar(
        select(CourseMember.id).where(
            CourseMember.user_id == user.id,
            CourseMember.status == MembershipStatus.ACTIVE,
            CourseMember.role.in_([CourseRole.TEACHER, CourseRole.TA]),
        )
    )
    if not is_admin(user) and course_staff is None:
        return _redirect("/me/reports")
    reports = list_reports_for_reviewer(db, user)
    return render_template(request, db, "admin_reports.html", {"reports": reports})


@router.post("/admin/reports/{report_id}")
def update_report(
    report_id: int,
    request: Request,
    status: str = Form(...),
    note: str = Form(""),
    redirect_to: str = Form("/admin/reports"),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    report = db.get(Report, report_id)
    if report is None or not can_handle_report(db, report, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/student/courses")
    try:
        update_report_status(db, report, handler=user, status=status, note=note)
        db.commit()
    except ValueError:
        db.rollback()
        push_flash(request, choose_text(request, "Invalid status.", "状态无效。"), "danger")
        return _redirect("/admin/reports")
    push_flash(request, choose_text(request, "Report updated.", "举报已更新。"), "success")
    return _redirect(safe_local_redirect(redirect_to, "/admin/reports"))
