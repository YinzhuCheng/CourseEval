"""User-created discussion groups."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import is_admin, push_flash
from app.constants import DiscussionGroupVisibility, DiscussionTopicKind
from app.db import get_db
from app.i18n import choose_text
from app.models import DiscussionGroup, DiscussionPost, DiscussionTopic
from app.services.discussion_attachments import attach_discussion_images_to_post, delete_discussion_attachment_files
from app.services.discussion_forms import extract_discussion_images
from app.services.discussion_groups import (
    add_group_member_by_username,
    build_group_discussion_context,
    can_manage_group,
    can_moderate_group,
    can_post_in_group,
    can_super_admin_review_private_group,
    can_view_group,
    create_group,
    freeze_group,
    group_cover_url,
    list_group_members,
    list_visible_groups,
    log_private_group_review,
    remove_group_member,
    set_group_member_mute,
    update_group_card,
)
from app.services.discussions import create_post, hard_delete_post
from app.services.image_uploads import normalize_uploaded_image
from app.services.permissions import RedirectRequired, require_user
from app.services.redirects import safe_local_redirect
from app.services.storage_paths import absolute_data_path, relative_to_data
from app.services.upload_limits import read_upload_file_limited
from app.services.user_storage import QuotaExceededError, record_stored_object
from app.web import render_template

router = APIRouter(tags=["discussion_groups"])


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _return_url(request: Request) -> str:
    q = getattr(request.url, "query", "") or ""
    return f"{request.url.path}?{q}" if q else request.url.path


def _via_report(request: Request) -> tuple[bool, int | None]:
    raw = request.query_params.get("report_id")
    if raw and raw.isdigit():
        return True, int(raw)
    return False, None


@router.get("/discussion-groups")
def discussion_group_home(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    groups = list_visible_groups(db, user)
    rows = []
    for g in groups:
        rows.append(
            {
                "group": g,
                "cover_url": group_cover_url(g),
                "can_manage": can_manage_group(db, g, user),
            }
        )
    return render_template(request, db, "discussion_group_home.html", {"group_cards": rows})


@router.get("/discussion-groups/help")
def discussion_group_help(request: Request, db: Session = Depends(get_db)):
    try:
        require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(request, db, "discussion_group_help.html", {})


@router.get("/discussion-groups/new")
def discussion_group_new(request: Request, db: Session = Depends(get_db)):
    try:
        require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(request, db, "discussion_group_form.html", {"group": None})


@router.post("/discussion-groups")
def discussion_group_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    visibility: str = Form(DiscussionGroupVisibility.PRIVATE.value),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    try:
        group = create_group(db, creator=user, title=title, description=description, visibility=visibility)
        db.commit()
    except ValueError:
        db.rollback()
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect("/discussion-groups/new")
    push_flash(request, choose_text(request, "Discussion group created.", "讨论组已创建。"), "success")
    return _redirect(f"/discussion-groups/{group.id}")


@router.get("/discussion-groups/{group_id}")
def discussion_group_detail(group_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    via_report, report_id = _via_report(request)
    if group is None or not can_view_group(db, group, user, via_report=via_report):
        push_flash(request, choose_text(request, "Discussion group not found.", "未找到讨论组。"), "danger")
        return _redirect("/discussion-groups")
    if via_report and can_super_admin_review_private_group(db, group, user):
        log_private_group_review(db, group=group, actor=user, report_id=report_id, detail="view_group_detail")
        db.commit()
    ctx = build_group_discussion_context(db, group=group, viewer=user, via_report=via_report)
    hidden_private_notice = (
        group.visibility == DiscussionGroupVisibility.PUBLIC
        and ctx["discussion_topic_post_count"] < len(group.discussion_topic.posts if group.discussion_topic else [])
    )
    return render_template(
        request,
        db,
        "discussion_group_detail.html",
        {
            "group": group,
            "group_cover_url": group_cover_url(group),
            "group_members": list_group_members(db, group.id),
            "can_manage_group": can_manage_group(db, group, user),
            "can_moderate_group": can_moderate_group(db, group, user, via_report=via_report),
            "can_freeze_group": can_moderate_group(db, group, user, via_report=via_report) and not can_manage_group(db, group, user),
            "hidden_private_notice": hidden_private_notice,
            "via_report": via_report,
            **ctx,
            "can_post_discussion": can_post_in_group(db, group, user),
            "discussion_post_url": f"/discussion-groups/{group.id}/discuss",
            "discussion_notice": "",
            "discussion_redirect_to": _return_url(request),
            "discussion_delete_action": f"/discussion-groups/{group.id}/discussion/delete-post",
        },
    )


@router.get("/discussion-groups/{group_id}/edit")
def discussion_group_edit(group_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    if group is None or not can_manage_group(db, group, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/discussion-groups/{group_id}")
    return render_template(request, db, "discussion_group_form.html", {"group": group})


@router.post("/discussion-groups/{group_id}")
def discussion_group_update(
    group_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    visibility: str = Form(DiscussionGroupVisibility.PRIVATE.value),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    if group is None or not can_manage_group(db, group, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/discussion-groups/{group_id}")
    try:
        update_group_card(db, group, title=title, description=description, visibility=visibility)
        db.commit()
    except ValueError:
        db.rollback()
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect(f"/discussion-groups/{group_id}/edit")
    push_flash(request, choose_text(request, "Discussion group updated.", "讨论组已更新。"), "success")
    return _redirect(f"/discussion-groups/{group_id}")


@router.post("/discussion-groups/{group_id}/cover")
async def discussion_group_cover(
    group_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    if group is None or not can_manage_group(db, group, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/discussion-groups/{group_id}")
    try:
        raw = await read_upload_file_limited(file)
        ext, cleaned = normalize_uploaded_image(raw, file.filename or "cover.png")
        upload_dir = absolute_data_path("uploads").parent / "uploads" / "discussion-groups" / f"group-{group.id}"
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored = upload_dir / f"cover-{group.id}{ext}"
        stored.write_bytes(cleaned)
        rel = relative_to_data(stored)
        record_stored_object(
            db,
            user_id=user.id,
            category="discussion_group_cover",
            relative_path=rel,
            size_bytes=stored.stat().st_size,
            ref_type="discussion_group",
            ref_id=group.id,
        )
        group.cover_image_path = rel
        db.commit()
    except (QuotaExceededError, ValueError):
        db.rollback()
        push_flash(request, choose_text(request, "Upload failed.", "上传失败。"), "danger")
        return _redirect(f"/discussion-groups/{group_id}/edit")
    push_flash(request, choose_text(request, "Cover image updated.", "封面已更新。"), "success")
    return _redirect(f"/discussion-groups/{group_id}/edit")


@router.post("/discussion-groups/{group_id}/members")
def discussion_group_add_member(
    group_id: int,
    request: Request,
    username: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    if group is None or not can_manage_group(db, group, user):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/discussion-groups/{group_id}")
    try:
        added = add_group_member_by_username(db, group, username)
        db.commit()
        push_flash(request, choose_text(request, f"Added {added.username}.", f"已添加 {added.username}。"), "success")
    except ValueError:
        db.rollback()
        push_flash(request, choose_text(request, "User not found.", "未找到用户。"), "danger")
    return _redirect(f"/discussion-groups/{group_id}")


@router.post("/discussion-groups/{group_id}/members/remove")
def discussion_group_remove_member(
    group_id: int,
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    if group is None or not can_manage_group(db, group, user):
        return _redirect(f"/discussion-groups/{group_id}")
    try:
        remove_group_member(db, group, user_id)
        db.commit()
    except ValueError:
        db.rollback()
    return _redirect(f"/discussion-groups/{group_id}")


@router.post("/discussion-groups/{group_id}/members/mute")
def discussion_group_mute_member(
    group_id: int,
    request: Request,
    user_id: int = Form(...),
    muted_until: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    if group is None or not can_manage_group(db, group, user):
        return _redirect(f"/discussion-groups/{group_id}")
    until_dt = None
    raw = (muted_until or "").strip()
    if raw:
        try:
            until_dt = datetime.fromisoformat(raw)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            push_flash(request, choose_text(request, "Invalid end time.", "结束时间无效。"), "danger")
            return _redirect(f"/discussion-groups/{group_id}")
    try:
        set_group_member_mute(db, group, user_id, until_dt)
        db.commit()
    except ValueError:
        db.rollback()
    return _redirect(f"/discussion-groups/{group_id}")


@router.post("/discussion-groups/{group_id}/freeze")
def discussion_group_freeze(
    group_id: int,
    request: Request,
    frozen: str = Form("1"),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    via_report, _ = _via_report(request)
    if group is None or not can_moderate_group(db, group, user, via_report=via_report) or (not is_admin(user) and not via_report):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(redirect_to or f"/discussion-groups/{group_id}")
    freeze_group(db, group, user, frozen == "1")
    db.commit()
    return _redirect(safe_local_redirect(redirect_to, f"/discussion-groups/{group_id}"))


@router.post("/discussion-groups/{group_id}/discuss")
async def discussion_group_discuss(
    group_id: int,
    request: Request,
    body: str = Form(""),
    parent_post_id: str = Form(""),
    anonymous: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    if group is None or not can_post_in_group(db, group, user) or group.discussion_topic is None:
        push_flash(request, choose_text(request, "You cannot post in this group.", "你无法在此讨论组发言。"), "danger")
        return _redirect(f"/discussion-groups/{group_id}")
    image_files = await extract_discussion_images(request)
    dest = safe_local_redirect(redirect_to, f"/discussion-groups/{group_id}")
    stored_paths: list[str] = []
    try:
        parent_id = int(parent_post_id) if str(parent_post_id or "").isdigit() else None
        post = create_post(
            db,
            topic_id=group.discussion_topic.id,
            author=user,
            body=body,
            parent_post_id=parent_id,
            is_anonymous=(anonymous == "on"),
            has_pending_image_uploads=bool(image_files),
        )
        post.visibility_snapshot = group.visibility.value
        stored_paths = attach_discussion_images_to_post(db, post, group.discussion_topic.course_id, image_files)
        db.commit()
    except Exception:
        db.rollback()
        delete_discussion_attachment_files(stored_paths)
        push_flash(request, choose_text(request, "Post failed.", "发布失败。"), "danger")
        return _redirect(dest)
    return _redirect(dest)


@router.post("/discussion-groups/{group_id}/discussion/delete-post")
def discussion_group_delete_post(
    group_id: int,
    request: Request,
    post_id: int = Form(...),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    group = db.get(DiscussionGroup, group_id)
    via_report, _ = _via_report(request)
    if group is None or not can_moderate_group(db, group, user, via_report=via_report):
        return _redirect(redirect_to or f"/discussion-groups/{group_id}")
    post = db.scalar(
        select(DiscussionPost)
        .join(DiscussionTopic, DiscussionPost.topic_id == DiscussionTopic.id)
        .where(
            DiscussionPost.id == post_id,
            DiscussionTopic.kind == DiscussionTopicKind.DISCUSSION_GROUP,
            DiscussionTopic.discussion_group_id == group.id,
        )
    )
    if post is not None:
        hard_delete_post(db, post, actor=user, course_id=post.topic.course_id)
        db.commit()
    return _redirect(safe_local_redirect(redirect_to, f"/discussion-groups/{group_id}"))

