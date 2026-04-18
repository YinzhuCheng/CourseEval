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
from app.services.discussions import (
    attach_avatar_and_role_badges,
    can_post_on_material_topic,
    create_post,
    display_label_for_post,
    get_or_create_material_topic,
    list_posts_for_topic,
    flat_thread_for_template,
)
from app.services.permissions import RedirectRequired, get_course_role, require_teacher_account, require_user
from app.web import render_template

router = APIRouter(tags=["course_content"])


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
    except ValueError:
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}/materials/new")
    push_flash(request, choose_text(request, "Material was saved.", "学习资料已保存。"), "success")
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
    except ValueError:
        push_flash(request, choose_text(request, "Title is required.", "标题不能为空。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}/edit")
    push_flash(request, choose_text(request, "Material was updated.", "学习资料已更新。"), "success")
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
    raw = await file.read()
    try:
        rel = store_material_image(course_id, material_id, raw, file.filename or "image.png")
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
    posts = list_posts_for_topic(db, topic.id)
    decorated = []
    for p in posts:
        label, hint = display_label_for_post(p, user, db, course_id)
        decorated.append({"post": p, "display_name": label, "staff_hint": hint})
    threaded = attach_avatar_and_role_badges(db, course_id, flat_thread_for_template(posts, decorated))
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
            "discussion_thread": threaded,
            "can_post_discussion": can_post,
            "is_teacher_view": True,
            "discussion_post_url": f"/teacher/courses/{course_id}/materials/{material_id}/discuss",
            "discussion_notice": "",
        },
    )


@router.post("/teacher/courses/{course_id}/materials/{material_id}/discuss")
def teacher_material_discuss(
    course_id: int,
    material_id: int,
    request: Request,
    body: str = Form(...),
    parent_post_id: str = Form(""),
    anonymous: str = Form(""),
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
    try:
        create_post(
            db,
            topic_id=topic.id,
            author=user,
            body=body,
            parent_post_id=pid,
            is_anonymous=(anonymous == "on" or anonymous == "true"),
        )
        db.commit()
    except ValueError:
        db.rollback()
        push_flash(request, choose_text(request, "Message cannot be empty.", "内容不能为空。"), "danger")
        return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}")
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    return _redirect(f"/teacher/courses/{course_id}/materials/{material_id}")


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
    posts = list_posts_for_topic(db, topic.id)
    decorated = []
    for p in posts:
        label, hint = display_label_for_post(p, user, db, course_id)
        decorated.append({"post": p, "display_name": label, "staff_hint": hint})
    threaded = attach_avatar_and_role_badges(db, course_id, flat_thread_for_template(posts, decorated))
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
            "discussion_thread": threaded,
            "can_post_discussion": can_post,
            "is_teacher_view": False,
            "discussion_post_url": f"/student/courses/{course_id}/materials/{material_id}/discuss",
            "discussion_notice": "",
        },
    )


@router.post("/student/courses/{course_id}/materials/{material_id}/discuss")
def student_material_discuss(
    course_id: int,
    material_id: int,
    request: Request,
    body: str = Form(...),
    parent_post_id: str = Form(""),
    anonymous: str = Form(""),
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
    try:
        create_post(
            db,
            topic_id=topic.id,
            author=user,
            body=body,
            parent_post_id=pid,
            is_anonymous=(anonymous == "on" or anonymous == "true"),
        )
        db.commit()
    except ValueError:
        db.rollback()
        push_flash(request, choose_text(request, "Message cannot be empty.", "内容不能为空。"), "danger")
        return _redirect(f"/student/courses/{course_id}/materials/{material_id}")
    push_flash(request, choose_text(request, "Posted.", "已发布。"), "success")
    return _redirect(f"/student/courses/{course_id}/materials/{material_id}")


