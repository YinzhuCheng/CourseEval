"""Course materials (teacher CRUD) and material viewing (student)."""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import push_flash
from app.constants import CourseRole
from app.db import get_db
from app.i18n import choose_text
from app.models import CourseMaterial
from app.services.course_materials import (
    create_material,
    get_material_for_course,
    list_materials_for_course,
    store_material_image,
    update_material,
)
from app.services.courses import get_course_for_staff, get_course_for_student
from app.services.discussion_ai import create_user_post_and_maybe_ai_reply
from app.services.discussion_attachments import attach_discussion_images_to_post, delete_discussion_attachment_files
from app.services.discussion_forms import extract_discussion_images
from app.services.image_uploads import format_image_upload_error
from app.services.discussions import (
    build_discussion_view_context,
    can_post_on_material_topic,
    get_or_create_material_topic,
)
from app.services.permissions import RedirectRequired, get_course_role, require_teacher_account, require_user
from app.services.redirects import safe_local_redirect
from app.services.upload_limits import read_upload_file_limited
from app.web import render_template

router = APIRouter(tags=["course_content"])


def _discussion_return_url(request: Request) -> str:
    q = getattr(request.url, "query", "") or ""
    return f"{request.url.path}?{q}" if q else request.url.path


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


# --- Teacher: materials list on course page is linked; full CRUD ---


@router.get("/teacher/courses/{course_id}/materials/new")
def teacher_new_material(course_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None or get_course_role(db, course.id, user.id) != CourseRole.TEACHER:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/teacher/courses")
    return render_template(request, db, "teacher_material_form.html", {"course": course, "material": None})


@router.post("/teacher/courses/{course_id}/materials")
def teacher_create_material(
    course_id: int,
    request: Request,
    title: str = Form(...),
    body_markdown: str = Form(""),
    external_url: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None or get_course_role(db, course_id, user.id) != CourseRole.TEACHER:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/teacher/courses")
    try:
        create_material(db, course=course, title=title, body_markdown=body_markdown, external_url=external_url, creator=user)
        db.commit()
    except ValueError as exc:
        msg = str(exc)
        push_flash(
            request,
            choose_text(
                request,
                "External URL must start with http:// or https://." if msg == "invalid_external_url" else "Title is required.",
                "外部链接必须以 http:// 或 https:// 开头。" if msg == "invalid_external_url" else "标题不能为空。",
            ),
            "danger",
        )
        return _redirect(f"/teacher/courses/{course_id}/materials/new")
    push_flash(request, choose_text(request, "Discussion item was saved.", "讨论已保存。"), "success")
    return _redirect(f"/teacher/courses/{course_id}")


@router.get("/teacher/courses/{course_id}/materials/{material_id}/edit")
def teacher_edit_material(course_id: int, material_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None or get_course_role(db, course_id, user.id) != CourseRole.TEACHER:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/teacher/courses")
    material = get_material_for_course(db, material_id, course_id)
    if material is None:
        return _redirect(f"/teacher/courses/{course_id}")
    return render_template(request, db, "teacher_material_form.html", {"course": course, "material": material})


@router.post("/teacher/courses/{course_id}/materials/{material_id}")
def teacher_update_material(
    course_id: int,
    material_id: int,
    request: Request,
    title: str = Form(...),
    body_markdown: str = Form(""),
    external_url: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None or get_course_role(db, course_id, user.id) != CourseRole.TEACHER:
        push_flash(request, choose_text(request, "Access denied.", "无权限。"), "danger")
        return _redirect("/teacher/courses")
    material = get_material_for_course(db, material_id, course_id)
    if material is None:
        return _redirect(f"/teacher/courses/{course_id}")
    try:
        update_material(db, material, title=title, body_markdown=body_markdown, external_url=external_url)
        db.commit()
    except ValueError as exc:
        msg = str(exc)
        push_flash(
            request,
            choose_text(
                request,
                "External URL must start with http:// or https://." if msg == "invalid_external_url" else "Title is required.",
                "外部链接必须以 http:// 或 https:// 开头。" if msg == "invalid_external_url" else "标题不能为空。",
            ),
            "danger",
        )
        return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}/edit")
    push_flash(request, choose_text(request, "Discussion item was updated.", "讨论已更新。"), "success")
    return _redirect(f"/teacher/courses/{course_id}")


@router.post("/teacher/courses/{course_id}/materials/{material_id}/upload-image")
async def teacher_upload_material_image(
    course_id: int,
    material_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None or get_course_role(db, course_id, user.id) != CourseRole.TEACHER:
        return _redirect("/teacher/courses")
    material = get_material_for_course(db, material_id, course_id)
    if material is None:
        return _redirect(f"/teacher/courses/{course_id}")
    try:
        raw = await read_upload_file_limited(file)
        rel = store_material_image(course_id, material_id, raw, file.filename or "image.png")
        from app.services.storage_paths import absolute_data_path
        from app.services.user_storage import QuotaExceededError, record_stored_object

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
    except QuotaExceededError:
        push_flash(request, choose_text(request, "Storage quota exceeded.", "存储空间已满，无法上传。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}/edit")
    except ValueError as e:
        push_flash(request, choose_text(request, str(e), "上传失败。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}/edit")
    from urllib.parse import quote

    public_path = f"/data-files/{quote(rel, safe='/')}"
    push_flash(
        request,
        choose_text(request, f"Image uploaded. Markdown: ![img]({public_path})", f"图片已上传，Markdown：![img]({public_path})"),
        "success",
    )
    return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}/edit")


# --- Student: view material ---


@router.get("/teacher/courses/{course_id}/materials/{material_id}")
def teacher_material_detail(course_id: int, material_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None:
        return _redirect("/teacher/courses")
    material = get_material_for_course(db, material_id, course_id)
    if material is None:
        return _redirect(f"/teacher/courses/{course_id}")
    topic = get_or_create_material_topic(db, material.id, course_id)
    db.commit()
    disc_ctx = build_discussion_view_context(db, topic_id=topic.id, course_id=course_id, viewer=user, request=request)
    can_post = can_post_on_material_topic(db, course_id, user)
    from app.services.markdown_sanitize import render_material_markdown

    body_html = render_material_markdown(material.body_markdown)
    return render_template(
        request,
        db,
        "student_material_detail.html",
        {
            "course": course,
            "material": material,
            "body_html": body_html,
            "topic_id": topic.id,
            **disc_ctx,
            "can_post_discussion": can_post,
            "is_teacher_view": True,
            "discussion_post_url": f"/teacher/courses/{course_id}/materials/{material_id}/discuss",
            "discussion_notice": "",
            "discussion_redirect_to": _discussion_return_url(request),
            "discussion_delete_action": f"/teacher/courses/{course_id}/discussion/delete-post",
        },
    )


@router.post("/teacher/courses/{course_id}/materials/{material_id}/discuss")
async def teacher_material_discuss(
    course_id: int,
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
        user = require_teacher_account(request, db)
        course = get_course_for_staff(db, course_id, user.id)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    if course is None:
        return _redirect("/teacher/courses")
    material = get_material_for_course(db, material_id, course_id)
    if material is None:
        return _redirect(f"/teacher/courses/{course_id}")
    if not can_post_on_material_topic(db, course_id, user):
        push_flash(request, choose_text(request, "You cannot post here.", "你无法在此发言。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}")
    topic = get_or_create_material_topic(db, material.id, course_id)
    db.commit()
    pid = int(parent_post_id) if parent_post_id.strip().isdigit() else None
    selected_group_id = int(ai_group_id) if ai_group_id.strip().isdigit() else None
    image_files = await extract_discussion_images(request)
    attachment_paths: list[str] = []
    default_dest = f"/teacher/courses/{course_id}/materials/{material_id}"
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
            try:
                attachment_paths = attach_discussion_images_to_post(db, _u, course_id, image_files)
            except ValueError as att_err:
                ak = str(att_err) if att_err else ""
                if ak == "too_many_images":
                    raise ValueError("too_many_images") from att_err
                if ak == "storage_quota_exceeded":
                    raise ValueError("storage_quota_exceeded") from att_err
                if ak in ("unsupported_image_type", "file_too_large"):
                    raise ValueError(ak) from att_err
                raise
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
        push_flash(request, msg, "danger")
        dest = safe_local_redirect(redirect_to, default_dest)
        return _redirect(dest)
    except Exception:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        raise
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    if ai_err:
        push_flash(request, choose_text(request, f"AI: {ai_err}", f"AI：{ai_err}"), "warning")
    dest = safe_local_redirect(redirect_to, default_dest)
    return _redirect(dest)


@router.get("/student/courses/{course_id}/materials/{material_id}")
def student_material_detail(course_id: int, material_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    course = get_course_for_student(db, course_id, user.id)
    if course is None:
        return _redirect("/student/courses")
    material = get_material_for_course(db, material_id, course_id)
    if material is None:
        return _redirect(f"/student/courses/{course_id}")
    topic = get_or_create_material_topic(db, material.id, course_id)
    db.commit()
    disc_ctx = build_discussion_view_context(db, topic_id=topic.id, course_id=course_id, viewer=user, request=request)
    can_post = can_post_on_material_topic(db, course_id, user)
    from app.services.markdown_sanitize import render_material_markdown

    body_html = render_material_markdown(material.body_markdown)
    return render_template(
        request,
        db,
        "student_material_detail.html",
        {
            "course": course,
            "material": material,
            "body_html": body_html,
            "topic_id": topic.id,
            **disc_ctx,
            "can_post_discussion": can_post,
            "is_teacher_view": False,
            "discussion_post_url": f"/student/courses/{course_id}/materials/{material_id}/discuss",
            "discussion_notice": "",
            "discussion_redirect_to": _discussion_return_url(request),
        },
    )


@router.post("/student/courses/{course_id}/materials/{material_id}/discuss")
async def student_material_discuss(
    course_id: int,
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
    course = get_course_for_student(db, course_id, user.id)
    if course is None:
        return _redirect("/student/courses")
    material = get_material_for_course(db, material_id, course_id)
    if material is None:
        return _redirect(f"/student/courses/{course_id}")
    if not can_post_on_material_topic(db, course_id, user):
        push_flash(request, choose_text(request, "You cannot post here.", "你无法在此发言。"), "danger")
        return _redirect(f"/student/courses/{course_id}/materials/{material_id}")
    topic = get_or_create_material_topic(db, material.id, course_id)
    db.commit()
    pid = int(parent_post_id) if parent_post_id.strip().isdigit() else None
    selected_group_id = int(ai_group_id) if ai_group_id.strip().isdigit() else None
    image_files = await extract_discussion_images(request)
    attachment_paths = []
    default_dest = f"/student/courses/{course_id}/materials/{material_id}"
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
            try:
                attachment_paths = attach_discussion_images_to_post(db, _u, course_id, image_files)
            except ValueError as att_err:
                ak = str(att_err) if att_err else ""
                if ak == "too_many_images":
                    raise ValueError("too_many_images") from att_err
                if ak == "storage_quota_exceeded":
                    raise ValueError("storage_quota_exceeded") from att_err
                if ak in ("unsupported_image_type", "file_too_large"):
                    raise ValueError(ak) from att_err
                raise
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
        push_flash(request, msg, "danger")
        dest = safe_local_redirect(redirect_to, default_dest)
        return _redirect(dest)
    except Exception:
        db.rollback()
        delete_discussion_attachment_files(attachment_paths)
        raise
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    if ai_err:
        push_flash(request, choose_text(request, f"AI: {ai_err}", f"AI：{ai_err}"), "warning")
    dest = safe_local_redirect(redirect_to, default_dest)
    return _redirect(dest)
