"""Platform open discussion area: topic cards (hidden open-community course as backing store)."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import push_flash
from app.config import get_settings
from app.db import get_db
from app.i18n import choose_text
from app.constants import DiscussionTopicKind
from app.models import CourseDiscussionMute, CourseMember, DiscussionPost, DiscussionTopic, User
from app.services.course_materials import (
    create_material,
    get_material_for_course,
    list_materials_for_course,
    store_material_image,
    update_material,
)
from app.services.discussion_ai import create_user_post_and_maybe_ai_reply
from app.services.discussion_attachments import attach_discussion_images_to_post, delete_discussion_attachment_files
from app.services.discussion_forms import extract_discussion_images
from app.services.discussions import (
    build_discussion_view_context,
    can_moderate_discussion,
    can_post_on_material_topic,
    get_or_create_material_topic,
    hard_delete_post,
    mute_user_in_course,
    unmute_user_in_course,
)
from app.services.free_discussion import (
    can_manage_free_topic,
    create_free_topic_card,
    get_discussion_topic_for_free_card,
    get_free_topic,
    get_open_community_course,
    list_free_topic_cards,
    update_free_topic_card,
)
from app.services.image_uploads import format_image_upload_error
from app.services.markdown_sanitize import render_material_markdown
from app.services.permissions import RedirectRequired, get_course_membership, require_user
from app.services.redirects import safe_local_redirect
from app.services.storage_paths import absolute_data_path
from app.services.user_storage import QuotaExceededError, record_stored_object
from app.web import render_template

router = APIRouter(tags=["free_discussion"])
settings = get_settings()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _discussion_return_url(request: Request) -> str:
    q = getattr(request.url, "query", "") or ""
    return f"{request.url.path}?{q}" if q else request.url.path


def _require_oc_member(db: Session, user: User):
    oc = get_open_community_course(db)
    if oc is None or get_course_membership(db, oc.id, user.id) is None:
        return None
    return oc


@router.get("/free-discussion")
def free_discussion_home(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    oc = _require_oc_member(db, user)
    if oc is None:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/student/courses")
    topics = list_free_topic_cards(db)
    topic_cards = []
    for t in topics:
        topic_cards.append(
            {
                "topic": t,
                "cover_url": f"/data-files/{quote(str(t.cover_image_path), safe='/')}" if t.cover_image_path else None,
            }
        )
    return render_template(
        request,
        db,
        "free_discussion_home.html",
        {"open_course": oc, "free_topic_cards": topic_cards},
    )


@router.get("/free-discussion/topics/new")
def free_topic_new_form(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    oc = _require_oc_member(db, user)
    if oc is None:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/student/courses")
    return render_template(request, db, "free_discussion_topic_form.html", {"open_course": oc, "free_topic": None})


@router.post("/free-discussion/topics")
def free_topic_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    oc = _require_oc_member(db, user)
    if oc is None:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/student/courses")
    try:
        ft = create_free_topic_card(db, course=oc, creator=user, title=title, description=description)
        db.commit()
    except ValueError:
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect("/free-discussion/topics/new")
    push_flash(request, choose_text(request, "Topic created.", "话题已创建。"), "success")
    return _redirect(f"/free-discussion/topics/{ft.id}")


@router.get("/free-discussion/topics/{topic_id}/edit")
def free_topic_edit_form(topic_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        push_flash(request, choose_text(request, "Topic not found.", "未找到话题。"), "danger")
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    return render_template(
        request,
        db,
        "free_discussion_topic_form.html",
        {"open_course": oc, "free_topic": ft},
    )


@router.post("/free-discussion/topics/{topic_id}")
def free_topic_update(
    topic_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    sort_order: str = Form("0"),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        push_flash(request, choose_text(request, "Topic not found.", "未找到话题。"), "danger")
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    try:
        so = int((sort_order or "0").strip() or "0")
    except ValueError:
        so = 0
    try:
        update_free_topic_card(db, ft, title=title, description=description, sort_order=so)
        db.commit()
    except ValueError:
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/edit")
    push_flash(request, choose_text(request, "Topic updated.", "话题已更新。"), "success")
    return _redirect(f"/free-discussion/topics/{topic_id}")


def store_free_topic_cover_image(course_id: int, free_topic_id: int, file_bytes: bytes, original_filename: str) -> str:
    from uuid import uuid4

    from app.services.image_uploads import normalize_uploaded_image
    from app.services.storage_paths import relative_to_data

    ext, cleaned = normalize_uploaded_image(file_bytes, original_filename)
    upload_dir = settings.uploads_dir / "free-discussion" / f"course-{course_id}" / f"topic-{free_topic_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"cover-{uuid4().hex}{ext}"
    stored.write_bytes(cleaned)
    return relative_to_data(stored)


@router.post("/free-discussion/topics/{topic_id}/cover")
async def free_topic_cover_upload(
    topic_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    raw = await file.read()
    try:
        rel = store_free_topic_cover_image(oc.id, ft.id, raw, file.filename or "cover.png")
        sz = absolute_data_path(rel).stat().st_size
        record_stored_object(
            db,
            user_id=user.id,
            category="free_discussion_cover",
            relative_path=rel,
            size_bytes=sz,
            ref_type="free_topic",
            ref_id=ft.id,
        )
        ft.cover_image_path = rel
        db.commit()
    except QuotaExceededError:
        push_flash(request, choose_text(request, "Storage quota exceeded.", "存储空间已满。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/edit")
    except ValueError as e:
        push_flash(request, choose_text(request, str(e), "上传失败。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/edit")
    push_flash(request, choose_text(request, "Cover image updated.", "封面已更新。"), "success")
    return _redirect(f"/free-discussion/topics/{topic_id}/edit")


@router.get("/free-discussion/topics/{topic_id}")
def free_topic_detail(topic_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        push_flash(request, choose_text(request, "Topic not found.", "未找到话题。"), "danger")
        return _redirect("/free-discussion")
    if _require_oc_member(db, user) is None:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/student/courses")
    disc_topic = get_discussion_topic_for_free_card(db, ft.id)
    if disc_topic is None:
        push_flash(request, choose_text(request, "Discussion not ready.", "讨论区未就绪。"), "danger")
        return _redirect("/free-discussion")
    disc_ctx = build_discussion_view_context(
        db,
        topic_id=disc_topic.id,
        course_id=oc.id,
        viewer=user,
        request=request,
        topic_owner_user_id=ft.created_by,
    )
    can_post = can_post_on_material_topic(db, oc.id, user)
    root_post = db.scalar(
        select(DiscussionPost)
        .where(
            DiscussionPost.topic_id == disc_topic.id,
            DiscussionPost.parent_post_id.is_(None),
            DiscussionPost.deleted_at.is_(None),
        )
        .order_by(DiscussionPost.created_at.asc())
        .limit(1)
    )
    root_body_html = render_material_markdown(root_post.body_text) if root_post else None
    if disc_ctx.get("discussion_thread") and root_post:
        disc_ctx["discussion_thread"] = [n for n in disc_ctx["discussion_thread"] if n["post"].id != root_post.id]
    chapters = [m for m in list_materials_for_course(db, oc.id) if (m.sort_order or 0) // 100000 == ft.id]
    mute_rows = list(db.scalars(select(CourseDiscussionMute).where(CourseDiscussionMute.course_id == oc.id)).all())
    discussion_mute_by_user = {m.user_id: m for m in mute_rows}
    cover_url = f"/data-files/{quote(str(ft.cover_image_path), safe='/')}" if ft.cover_image_path else None
    members = list(
        db.scalars(
            select(CourseMember)
            .options(joinedload(CourseMember.user))
            .where(CourseMember.course_id == oc.id)
            .order_by(CourseMember.joined_at.asc())
        ).all()
    )
    return render_template(
        request,
        db,
        "free_discussion_topic_detail.html",
        {
            "open_course": oc,
            "free_topic": ft,
            "topic_id": disc_topic.id,
            "can_manage_free_topic": can_manage_free_topic(db, user, ft),
            "chapters": chapters,
            "discussion_mute_by_user": discussion_mute_by_user,
            "free_topic_members": members,
            "can_moderate_open_discussion": can_moderate_discussion(db, oc.id, user),
            **disc_ctx,
            "can_post_discussion": can_post,
            "discussion_post_url": f"/free-discussion/topics/{topic_id}/discuss",
            "discussion_notice": "",
            "discussion_redirect_to": _discussion_return_url(request),
            "discussion_delete_action": "/free-discussion/discussion/delete-post",
            "root_post": root_post,
            "root_body_html": root_body_html,
            "free_topic_cover_url": cover_url,
        },
    )


@router.post("/free-discussion/topics/{topic_id}/discuss")
async def free_topic_discuss(
    topic_id: int,
    request: Request,
    body: str = Form(""),
    parent_post_id: str = Form(""),
    anonymous: str = Form(""),
    request_ai: str = Form(""),
    ai_group_id: str = Form(""),
    ai_context_mode: str = Form("recent_k"),
    ai_context_k: str = Form("1"),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if _require_oc_member(db, user) is None:
        return _redirect("/login")
    disc_topic = get_discussion_topic_for_free_card(db, ft.id)
    if disc_topic is None:
        return _redirect("/free-discussion")
    if not can_post_on_material_topic(db, oc.id, user):
        push_flash(request, choose_text(request, "You cannot post here.", "你无法在此发言。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    pid = int(parent_post_id) if parent_post_id.strip().isdigit() else None
    selected_group_id = int(ai_group_id) if ai_group_id.strip().isdigit() else None
    try:
        image_files = await extract_discussion_images(request)
    except ValueError as e:
        push_flash(request, choose_text(request, str(e), "图片处理失败。"), "danger")
        return _redirect(safe_local_redirect(redirect_to, f"/free-discussion/topics/{topic_id}"))
    attachment_paths: list[str] = []
    default_dest = f"/free-discussion/topics/{topic_id}"
    try:
        _u, ai_err = create_user_post_and_maybe_ai_reply(
            db,
            topic=disc_topic,
            user=user,
            body=body,
            parent_post_id=pid,
            is_anonymous=(anonymous == "on" or anonymous == "true"),
            request_ai=(request_ai == "on" or request_ai == "true"),
            pending_image_uploads=bool(image_files),
            selected_llm_group_id=selected_group_id,
            ai_context_mode=ai_context_mode,
            ai_context_k=ai_context_k,
        )
        if _u is not None and image_files:
            attachment_paths = attach_discussion_images_to_post(db, _u, oc.id, image_files)
        db.commit()
    except ValueError as exc:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        key = str(exc) if exc else ""
        if key == "user_muted":
            msg = choose_text(
                request,
                "You are muted from posting in this course discussion.",
                "你已被禁止在本课程讨论区发言。",
            )
        elif key == "body_too_large":
            msg = choose_text(request, "Message is too long.", "内容过长。")
        elif key == "remote_images_not_allowed":
            msg = choose_text(
                request,
                "Remote images in markdown are not allowed; use uploads instead.",
                "不允许在 Markdown 中嵌入外链图片，请使用上传图片。",
            )
        elif key == "too_many_images":
            msg = choose_text(request, "Too many images for one post.", "单条帖子图片数量超过上限。")
        elif key == "storage_quota_exceeded":
            msg = choose_text(request, "Storage quota exceeded.", "存储空间已满，无法上传图片。")
        elif key in ("unsupported_image_type", "file_too_large"):
            msg = format_image_upload_error(request, key)
        elif key == "nested_reply_not_allowed":
            msg = choose_text(request, "Nested replies are not allowed here.", "本区不支持多层嵌套回复。")
        else:
            msg = choose_text(request, "Message cannot be empty.", "内容不能为空。")
        dest = safe_local_redirect(redirect_to, default_dest)
        push_flash(request, msg, "danger")
        return _redirect(dest)
    except Exception:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        raise
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    if ai_err:
        push_flash(request, choose_text(request, f"AI: {ai_err}", f"AI：{ai_err}"), "warning")
    return _redirect(safe_local_redirect(redirect_to, default_dest))


@router.post("/free-discussion/discussion/delete-post")
def free_discussion_delete_post(
    request: Request,
    post_id: int = Form(...),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    oc = get_open_community_course(db)
    if oc is None:
        return _redirect("/free-discussion")
    post = db.get(DiscussionPost, post_id)
    if post is None or post.deleted_at is not None:
        push_flash(request, choose_text(request, "Post not found.", "未找到帖子。"), "danger")
        return _redirect(safe_local_redirect(redirect_to, "/free-discussion"))
    topic = db.get(DiscussionTopic, post.topic_id)
    if topic is None or topic.course_id != oc.id:
        push_flash(request, choose_text(request, "Post not found.", "未找到帖子。"), "danger")
        return _redirect(safe_local_redirect(redirect_to, "/free-discussion"))
    ft = get_free_topic(db, topic.free_discussion_topic_id) if topic.free_discussion_topic_id else None
    from app.services.discussions import can_moderate_discussion, can_moderate_free_topic_as_owner

    allowed = can_moderate_discussion(db, oc.id, user)
    if not allowed and ft is not None and can_moderate_free_topic_as_owner(db, oc.id, user, ft.created_by):
        if topic.kind == DiscussionTopicKind.FREE_DISCUSSION_TOPIC and post.parent_post_id is None:
            allowed = False
        else:
            allowed = True
    if not allowed:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(safe_local_redirect(redirect_to, "/free-discussion"))
    hard_delete_post(db, post, actor=user, course_id=oc.id)
    db.commit()
    push_flash(request, choose_text(request, "Post deleted.", "帖子已删除。"), "success")
    return _redirect(safe_local_redirect(redirect_to, "/free-discussion"))


@router.get("/free-discussion/topics/{topic_id}/chapters/new")
def free_chapter_new_form(topic_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    return render_template(
        request,
        db,
        "teacher_material_form.html",
        {
            "course": oc,
            "material": None,
            "form_action_create": f"/free-discussion/topics/{topic_id}/chapters",
            "form_action_update": None,
            "back_url": f"/free-discussion/topics/{topic_id}",
        },
    )


@router.post("/free-discussion/topics/{topic_id}/chapters")
def free_chapter_create(
    topic_id: int,
    request: Request,
    title: str = Form(...),
    body_markdown: str = Form(""),
    external_url: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    try:
        m = create_material(db, course=oc, title=title, body_markdown=body_markdown, external_url=external_url, creator=user)
        m.sort_order = ft.id * 100000 + m.id
        db.commit()
    except ValueError:
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/chapters/new")
    push_flash(request, choose_text(request, "Chapter saved.", "章节已保存。"), "success")
    return _redirect(f"/free-discussion/topics/{topic_id}/chapters/{m.id}")


@router.get("/free-discussion/topics/{topic_id}/chapters/{material_id}/edit")
def free_chapter_edit(topic_id: int, material_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    m = get_material_for_course(db, material_id, oc.id if oc else -1)
    if ft is None or oc is None or m is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    return render_template(
        request,
        db,
        "teacher_material_form.html",
        {
            "course": oc,
            "material": m,
            "form_action_create": None,
            "form_action_update": f"/free-discussion/topics/{topic_id}/chapters/{material_id}",
            "back_url": f"/free-discussion/topics/{topic_id}",
        },
    )


@router.post("/free-discussion/topics/{topic_id}/chapters/{material_id}")
def free_chapter_update(
    topic_id: int,
    material_id: int,
    request: Request,
    title: str = Form(...),
    body_markdown: str = Form(""),
    external_url: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    m = get_material_for_course(db, material_id, oc.id if oc else -1)
    if ft is None or oc is None or m is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    try:
        update_material(db, m, title=title, body_markdown=body_markdown, external_url=external_url)
        db.commit()
    except ValueError:
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/chapters/{material_id}/edit")
    push_flash(request, choose_text(request, "Chapter updated.", "章节已更新。"), "success")
    return _redirect(f"/free-discussion/topics/{topic_id}/chapters/{material_id}")


@router.post("/free-discussion/topics/{topic_id}/chapters/{material_id}/upload-image")
async def free_chapter_upload_image(
    topic_id: int,
    material_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    m = get_material_for_course(db, material_id, oc.id if oc else -1)
    if ft is None or oc is None or m is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        return _redirect(f"/free-discussion/topics/{topic_id}")
    raw = await file.read()
    try:
        rel = store_material_image(oc.id, material_id, raw, file.filename or "image.png")
        sz = absolute_data_path(rel).stat().st_size
        record_stored_object(
            db,
            user_id=user.id,
            category="course_material_image",
            relative_path=rel,
            size_bytes=sz,
            ref_type="material",
            ref_id=material_id,
        )
        db.commit()
    except QuotaExceededError:
        push_flash(request, choose_text(request, "Storage quota exceeded.", "存储空间已满。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/chapters/{material_id}/edit")
    except ValueError as e:
        push_flash(request, choose_text(request, str(e), "上传失败。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/chapters/{material_id}/edit")
    public_path = f"/data-files/{quote(rel, safe='/')}"
    push_flash(
        request,
        choose_text(
            request,
            f"Image uploaded. Markdown: ![img]({public_path})",
            f"图片已上传，Markdown：![img]({public_path})",
        ),
        "success",
    )
    return _redirect(f"/free-discussion/topics/{topic_id}/chapters/{material_id}/edit")


@router.get("/free-discussion/topics/{topic_id}/chapters/{material_id}")
def free_chapter_detail(topic_id: int, material_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    m = get_material_for_course(db, material_id, oc.id if oc else -1)
    if ft is None or oc is None or m is None or ft.course_id != oc.id:
        push_flash(request, choose_text(request, "Not found.", "未找到。"), "danger")
        return _redirect("/free-discussion")
    if _require_oc_member(db, user) is None:
        return _redirect("/login")
    if (m.sort_order or 0) // 100000 != ft.id:
        push_flash(request, choose_text(request, "Not found.", "未找到。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    topic = get_or_create_material_topic(db, m.id, oc.id)
    db.commit()
    disc_ctx = build_discussion_view_context(db, topic_id=topic.id, course_id=oc.id, viewer=user, request=request)
    can_post = can_post_on_material_topic(db, oc.id, user)
    body_html = render_material_markdown(m.body_markdown)
    return render_template(
        request,
        db,
        "student_material_detail.html",
        {
            "course": oc,
            "material": m,
            "body_html": body_html,
            "topic_id": topic.id,
            **disc_ctx,
            "can_post_discussion": can_post,
            "is_teacher_view": can_manage_free_topic(db, user, ft),
            "discussion_post_url": f"/free-discussion/topics/{topic_id}/chapters/{material_id}/discuss",
            "discussion_notice": "",
            "discussion_redirect_to": _discussion_return_url(request),
            "discussion_delete_action": "/free-discussion/discussion/delete-post",
            "back_to_course_url": f"/free-discussion/topics/{topic_id}",
            "back_to_course_label_en": "Back to topic",
            "back_to_course_label_zh": "返回话题",
        },
    )


@router.post("/free-discussion/topics/{topic_id}/chapters/{material_id}/discuss")
async def free_chapter_discuss(
    topic_id: int,
    material_id: int,
    request: Request,
    body: str = Form(""),
    parent_post_id: str = Form(""),
    anonymous: str = Form(""),
    request_ai: str = Form(""),
    ai_group_id: str = Form(""),
    ai_context_mode: str = Form("recent_k"),
    ai_context_k: str = Form("1"),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    m = get_material_for_course(db, material_id, oc.id if oc else -1)
    if ft is None or oc is None or m is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if (m.sort_order or 0) // 100000 != ft.id:
        return _redirect("/free-discussion")
    if not can_post_on_material_topic(db, oc.id, user):
        push_flash(request, choose_text(request, "You cannot post here.", "你无法在此发言。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}/chapters/{material_id}")
    topic = get_or_create_material_topic(db, m.id, oc.id)
    db.commit()
    pid = int(parent_post_id) if parent_post_id.strip().isdigit() else None
    selected_group_id = int(ai_group_id) if ai_group_id.strip().isdigit() else None
    try:
        image_files = await extract_discussion_images(request)
    except ValueError as e:
        push_flash(request, choose_text(request, str(e), "图片处理失败。"), "danger")
        return _redirect(
            safe_local_redirect(redirect_to, f"/free-discussion/topics/{topic_id}/chapters/{material_id}")
        )
    attachment_paths: list[str] = []
    default_dest = f"/free-discussion/topics/{topic_id}/chapters/{material_id}"
    try:
        _u, ai_err = create_user_post_and_maybe_ai_reply(
            db,
            topic=topic,
            user=user,
            body=body,
            parent_post_id=pid,
            is_anonymous=(anonymous == "on" or anonymous == "true"),
            request_ai=(request_ai == "on" or request_ai == "true"),
            pending_image_uploads=bool(image_files),
            selected_llm_group_id=selected_group_id,
            ai_context_mode=ai_context_mode,
            ai_context_k=ai_context_k,
        )
        if _u is not None and image_files:
            attachment_paths = attach_discussion_images_to_post(db, _u, oc.id, image_files)
        db.commit()
    except ValueError as exc:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        key = str(exc) if exc else ""
        if key == "user_muted":
            msg = choose_text(
                request,
                "You are muted from posting in this course discussion.",
                "你已被禁止在本课程讨论区发言。",
            )
        elif key == "body_too_large":
            msg = choose_text(request, "Message is too long.", "内容过长。")
        elif key == "remote_images_not_allowed":
            msg = choose_text(
                request,
                "Remote images in markdown are not allowed; use uploads instead.",
                "不允许在 Markdown 中嵌入外链图片，请使用上传图片。",
            )
        elif key == "too_many_images":
            msg = choose_text(request, "Too many images for one post.", "单条帖子图片数量超过上限。")
        elif key == "storage_quota_exceeded":
            msg = choose_text(request, "Storage quota exceeded.", "存储空间已满，无法上传图片。")
        elif key in ("unsupported_image_type", "file_too_large"):
            msg = format_image_upload_error(request, key)
        else:
            msg = choose_text(request, "Message cannot be empty.", "内容不能为空。")
        dest = safe_local_redirect(redirect_to, default_dest)
        push_flash(request, msg, "danger")
        return _redirect(dest)
    except Exception:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        raise
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    if ai_err:
        push_flash(request, choose_text(request, f"AI: {ai_err}", f"AI：{ai_err}"), "warning")
    return _redirect(safe_local_redirect(redirect_to, default_dest))


@router.post("/free-discussion/topics/{topic_id}/mute-user")
def free_topic_mute_user(
    topic_id: int,
    request: Request,
    user_id: int = Form(...),
    muted_until: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    target = db.get(User, user_id)
    if target is None:
        push_flash(request, choose_text(request, "User not found.", "用户不存在。"), "danger")
        return _redirect(safe_local_redirect(redirect_to, f"/free-discussion/topics/{topic_id}"))
    until_dt = None
    raw = (muted_until or "").strip()
    if raw:
        try:
            until_dt = datetime.fromisoformat(raw)
        except ValueError:
            push_flash(request, choose_text(request, "Invalid end time.", "结束时间无效。"), "danger")
            return _redirect(safe_local_redirect(redirect_to, f"/free-discussion/topics/{topic_id}"))
    mute_user_in_course(db, course_id=oc.id, target_user_id=target.id, actor=user, muted_until=until_dt)
    db.commit()
    push_flash(request, choose_text(request, "User muted.", "已禁言该用户。"), "success")
    return _redirect(safe_local_redirect(redirect_to, f"/free-discussion/topics/{topic_id}"))


@router.post("/free-discussion/topics/{topic_id}/unmute-user")
def free_topic_unmute_user(
    topic_id: int,
    request: Request,
    user_id: int = Form(...),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    ft = get_free_topic(db, topic_id)
    oc = get_open_community_course(db)
    if ft is None or oc is None or ft.course_id != oc.id:
        return _redirect("/free-discussion")
    if not can_manage_free_topic(db, user, ft):
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect(f"/free-discussion/topics/{topic_id}")
    unmute_user_in_course(db, course_id=oc.id, target_user_id=user_id, actor=user)
    db.commit()
    push_flash(request, choose_text(request, "Mute removed.", "已解除禁言。"), "success")
    return _redirect(safe_local_redirect(redirect_to, f"/free-discussion/topics/{topic_id}"))
