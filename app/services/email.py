import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import quote

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import get_settings
from app.db import utcnow
from app.i18n import choose_text
from app.models import EmailDeliveryLog, User


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDeliveryResult:
    delivered: bool
    url: str = ""
    error_message: str = ""


def build_action_url(request: Request, path: str, token: str) -> str:
    settings = get_settings()
    base_url = settings.app_base_url.rstrip("/") if settings.app_base_url else str(request.base_url).rstrip("/")
    return f"{base_url}{path}?token={quote(token)}"


def build_verification_url(request: Request, token: str) -> str:
    return build_action_url(request, "/verify-email", token)


def build_password_reset_url(request: Request, token: str) -> str:
    return build_action_url(request, "/reset-password", token)


def _record_email_delivery(
    db: Session | None,
    *,
    user: User | None,
    recipient: str,
    subject: str,
    purpose: str,
    delivered: bool,
    error_message: str = "",
) -> None:
    if db is None:
        return
    db.add(
        EmailDeliveryLog(
            user_id=user.id if user else None,
            recipient=recipient,
            subject=subject,
            purpose=purpose,
            delivered=delivered,
            error_message=error_message or None,
            created_at=utcnow(),
        )
    )
    db.commit()


def send_email(
    *,
    recipient: str,
    subject: str,
    body: str,
    purpose: str,
    db: Session | None = None,
    user: User | None = None,
) -> EmailDeliveryResult:
    settings = get_settings()

    if not settings.smtp_host or not settings.smtp_from_address:
        logger.warning("SMTP is not configured. Email purpose=%s recipient=%s", purpose, recipient)
        _record_email_delivery(
            db,
            user=user,
            recipient=recipient,
            subject=subject,
            purpose=purpose,
            delivered=False,
            error_message="SMTP is not configured.",
        )
        return EmailDeliveryResult(delivered=False, error_message="SMTP is not configured.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (
        formataddr((settings.smtp_from_name, settings.smtp_from_address))
        if settings.smtp_from_name
        else settings.smtp_from_address
    )
    message["To"] = recipient
    message.set_content(body)

    try:
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
    except Exception as exc:
        error_message = str(exc) or exc.__class__.__name__
        logger.exception("Failed to send %s email to %s", purpose, recipient)
        _record_email_delivery(
            db,
            user=user,
            recipient=recipient,
            subject=subject,
            purpose=purpose,
            delivered=False,
            error_message=error_message,
        )
        return EmailDeliveryResult(delivered=False, error_message=error_message)

    _record_email_delivery(
        db,
        user=user,
        recipient=recipient,
        subject=subject,
        purpose=purpose,
        delivered=True,
    )
    return EmailDeliveryResult(delivered=True)


def send_verification_email(request: Request, user: User, token: str, db: Session | None = None) -> EmailDeliveryResult:
    verification_url = build_verification_url(request, token)
    subject = choose_text(request, "Verify your CourseEval email", "验证你的 CourseEval 邮箱")
    body = choose_text(
        request,
        (
            f"Hello {user.username},\n\n"
            "Please click the link below to verify your email address and finish registration:\n"
            f"{verification_url}\n\n"
            "If you did not create this account, you can ignore this email."
        ),
        (
            f"{user.username}，你好：\n\n"
            "请点击下面的链接验证你的邮箱并完成注册：\n"
            f"{verification_url}\n\n"
            "如果这不是你发起的注册请求，可以直接忽略这封邮件。"
        ),
    )

    result = send_email(
        recipient=user.email,
        subject=subject,
        body=body,
        purpose="email_verification",
        db=db,
        user=user,
    )
    if not result.delivered:
        logger.warning("Email verification link for %s: %s", user.email, verification_url)
    return EmailDeliveryResult(
        delivered=result.delivered,
        url=verification_url,
        error_message=result.error_message,
    )


def send_password_reset_email(request: Request, user: User, token: str, db: Session | None = None) -> EmailDeliveryResult:
    reset_url = build_password_reset_url(request, token)
    subject = choose_text(request, "Reset your CourseEval password", "重置你的 CourseEval 密码")
    body = choose_text(
        request,
        (
            f"Hello {user.username},\n\n"
            "Use the link below to reset your CourseEval password:\n"
            f"{reset_url}\n\n"
            "This link expires in 2 hours. If you did not request a password reset, you can ignore this email."
        ),
        (
            f"{user.username}，你好：\n\n"
            "请使用下面的链接重置你的 CourseEval 密码：\n"
            f"{reset_url}\n\n"
            "该链接将在 2 小时后过期。如果这不是你发起的操作，可以直接忽略这封邮件。"
        ),
    )
    result = send_email(
        recipient=user.email,
        subject=subject,
        body=body,
        purpose="password_reset",
        db=db,
        user=user,
    )
    return EmailDeliveryResult(delivered=result.delivered, url=reset_url, error_message=result.error_message)


def send_smtp_test_email(request: Request, recipient: str, db: Session | None = None, user: User | None = None) -> EmailDeliveryResult:
    subject = choose_text(request, "CourseEval SMTP test", "CourseEval SMTP 测试")
    body = choose_text(
        request,
        "This is a CourseEval SMTP test email. If you received it, outbound email delivery is working.",
        "这是一封 CourseEval SMTP 测试邮件。如果你收到了它，说明邮件投递配置可用。",
    )
    return send_email(recipient=recipient, subject=subject, body=body, purpose="smtp_test", db=db, user=user)
