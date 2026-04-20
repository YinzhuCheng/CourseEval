"""Personal social center: friends, messages, invites, notifications, and privacy settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.auth import push_flash
from app.constants import SocialInviteType
from app.db import get_db, utcnow
from app.i18n import choose_text
from app.services.permissions import RedirectRequired, require_user
from app.services.redirects import safe_local_redirect
from app.services.social import (
    create_social_invite,
    list_friends,
    list_message_threads,
    list_notifications,
    list_received_friend_requests,
    list_received_invites,
    list_sent_friend_requests,
    list_sent_invites,
    mark_notifications_read,
    remove_friend,
    respond_friend_request,
    respond_social_invite,
    search_users_for_friend_request,
    send_direct_message,
    send_friend_request,
)
from app.web import render_template


router = APIRouter(tags=["social"])


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _flash_error(request: Request, code: str) -> None:
    messages = {
        "user_not_found": ("User not found.", "未找到用户。"),
        "cannot_friend_self": ("You cannot add yourself.", "不能添加自己。"),
        "already_friends": ("You are already friends.", "你们已经是好友。"),
        "request_pending": ("A friend request is already pending.", "已有待处理的好友申请。"),
        "request_not_found": ("Friend request not found.", "未找到好友申请。"),
        "friendship_not_found": ("Friendship not found.", "未找到好友关系。"),
        "not_friends": ("Only friends can use this action.", "只有好友之间可以执行此操作。"),
        "empty_message": ("Message cannot be empty.", "消息不能为空。"),
        "message_too_long": ("Message is too long.", "消息过长。"),
        "cannot_invite_self": ("You cannot invite yourself.", "不能邀请自己。"),
        "target_not_found": ("Target not found or access denied.", "目标不存在或无权限。"),
        "already_member": ("This user is already a member.", "该用户已经是成员。"),
        "invite_pending": ("An invitation is already pending.", "已有待处理的邀请。"),
        "privacy_restricted": ("This user only accepts discussion group invites from friends.", "该用户仅接受好友发来的讨论组邀请。"),
        "invite_not_found": ("Invitation not found.", "未找到邀请。"),
    }
    en, zh = messages.get(code, ("Action failed.", "操作失败。"))
    push_flash(request, choose_text(request, en, zh), "danger")


@router.get("/me/notifications")
def notifications_page(
    request: Request,
    category: str = Query("all"),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(
        request,
        db,
        "profile_notifications.html",
        {
            "profile_section": "notifications",
            "notification_category": category,
            "notifications": list_notifications(db, user.id, category=category),
        },
    )


@router.post("/me/notifications/read")
def notifications_mark_read(request: Request, category: str = Form("all"), db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    mark_notifications_read(db, user.id)
    db.commit()
    return _redirect(f"/me/notifications?category={category}")


@router.get("/me/friends")
def friends_page(request: Request, q: str = Query(""), db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(
        request,
        db,
        "profile_friends.html",
        {
            "profile_section": "friends",
            "friend_subsection": "list",
            "friends": list_friends(db, user.id),
            "query": q,
            "search_results": search_users_for_friend_request(db, viewer=user, query=q),
        },
    )


@router.get("/me/friends/requests")
def friend_requests_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(
        request,
        db,
        "profile_friend_requests.html",
        {
            "profile_section": "friends",
            "friend_subsection": "requests",
            "received_requests": list_received_friend_requests(db, user.id),
            "sent_requests": list_sent_friend_requests(db, user.id),
        },
    )


@router.post("/me/friends/requests")
def friend_request_create(
    request: Request,
    username: str = Form(...),
    message: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
        send_friend_request(db, requester=user, recipient_username=username, message=message)
        db.commit()
        push_flash(request, choose_text(request, "Friend request sent.", "好友申请已发送。"), "success")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect("/me/friends/requests")


@router.post("/me/friends/requests/{request_id}")
def friend_request_respond(
    request_id: int,
    request: Request,
    action: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
        respond_friend_request(db, request_id=request_id, recipient=user, accept=(action == "accept"))
        db.commit()
        push_flash(request, choose_text(request, "Friend request updated.", "好友申请已更新。"), "success")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect("/me/friends/requests")


@router.post("/me/friends/remove")
def friend_remove(request: Request, friend_id: int = Form(...), db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
        remove_friend(db, user=user, friend_id=friend_id)
        db.commit()
        push_flash(request, choose_text(request, "Friend removed.", "好友已删除。"), "success")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect("/me/friends")


@router.get("/me/messages")
def messages_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(
        request,
        db,
        "profile_messages.html",
        {
            "profile_section": "messages",
            "threads": list_message_threads(db, user.id),
            "friends": list_friends(db, user.id),
            "active_thread": None,
            "other_user": None,
        },
    )


@router.get("/me/messages/threads/{thread_id}")
def message_thread_page(thread_id: int, request: Request, db: Session = Depends(get_db)):
    from app.services.social import get_message_thread_for_user

    try:
        user = require_user(request, db)
        thread = get_message_thread_for_user(db, thread_id=thread_id, user=user)
        db.commit()
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError:
        push_flash(request, choose_text(request, "Conversation not found.", "未找到会话。"), "danger")
        return _redirect("/me/messages")
    other = thread.user_high if thread.user_low_id == user.id else thread.user_low
    return render_template(
        request,
        db,
        "profile_messages.html",
        {
            "profile_section": "messages",
            "threads": list_message_threads(db, user.id),
            "friends": list_friends(db, user.id),
            "active_thread": thread,
            "other_user": other,
        },
    )


@router.post("/me/messages")
def message_send(
    request: Request,
    recipient_id: int = Form(...),
    body: str = Form(...),
    thread_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
        msg = send_direct_message(db, sender=user, recipient_id=recipient_id, body=body)
        db.commit()
        return _redirect(f"/me/messages/threads/{msg.thread_id}")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect(f"/me/messages/threads/{thread_id}" if thread_id else "/me/messages")


@router.get("/me/invites")
def invites_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(
        request,
        db,
        "profile_invites.html",
        {
            "profile_section": "invites",
            "received_invites": list_received_invites(db, user.id),
            "sent_invites": list_sent_invites(db, user.id),
        },
    )


@router.post("/me/invites/{invite_id}")
def invite_respond(
    invite_id: int,
    request: Request,
    action: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
        respond_social_invite(db, invite_id=invite_id, invitee=user, accept=(action == "accept"))
        db.commit()
        push_flash(request, choose_text(request, "Invitation updated.", "邀请已更新。"), "success")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect("/me/invites")


@router.get("/me/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    return render_template(request, db, "profile_settings.html", {"profile_section": "settings", "profile_user": user})


@router.post("/me/settings/privacy")
def privacy_settings_update(
    request: Request,
    only_friends_can_invite_discussion_groups: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    user.only_friends_can_invite_discussion_groups = only_friends_can_invite_discussion_groups == "on"
    user.updated_at = utcnow()
    db.commit()
    push_flash(request, choose_text(request, "Settings saved.", "设置已保存。"), "success")
    return _redirect("/me/settings")


@router.post("/student/courses/{course_id}/invite")
def invite_to_course(
    course_id: int,
    request: Request,
    username: str = Form(...),
    message: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
        create_social_invite(
            db,
            inviter=user,
            invitee_username=username,
            invite_type=SocialInviteType.COURSE,
            target_id=course_id,
            message=message,
        )
        db.commit()
        push_flash(request, choose_text(request, "Invitation sent.", "邀请已发送。"), "success")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect(safe_local_redirect(redirect_to, f"/student/courses/{course_id}"))


@router.post("/free-discussion/topics/{topic_id}/invite")
def invite_to_free_topic(
    topic_id: int,
    request: Request,
    username: str = Form(...),
    message: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
        create_social_invite(
            db,
            inviter=user,
            invitee_username=username,
            invite_type=SocialInviteType.FREE_DISCUSSION_TOPIC,
            target_id=topic_id,
            message=message,
        )
        db.commit()
        push_flash(request, choose_text(request, "Invitation sent.", "邀请已发送。"), "success")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect(safe_local_redirect(redirect_to, f"/free-discussion/topics/{topic_id}"))


@router.post("/discussion-groups/{group_id}/invite")
def invite_to_discussion_group(
    group_id: int,
    request: Request,
    username: str = Form(...),
    message: str = Form(""),
    redirect_to: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        user = require_user(request, db)
        create_social_invite(
            db,
            inviter=user,
            invitee_username=username,
            invite_type=SocialInviteType.DISCUSSION_GROUP,
            target_id=group_id,
            message=message,
        )
        db.commit()
        push_flash(request, choose_text(request, "Invitation sent.", "邀请已发送。"), "success")
    except RedirectRequired as redirect:
        return _redirect(redirect.location)
    except ValueError as exc:
        db.rollback()
        _flash_error(request, str(exc))
    return _redirect(safe_local_redirect(redirect_to, f"/discussion-groups/{group_id}"))
