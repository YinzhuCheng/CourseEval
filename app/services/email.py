import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import quote

from starlette.requests import Request

from app.config import get_settings
from app.i18n import choose_text
from app.models import User


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDeliveryResult:
    delivered: bool
    verification_url: str


def build_verification_url(request: Request, token: str) -> str:
    settings = get_settings()
    base_url = settings.app_base_url.rstrip("/") if settings.app_base_url else str(request.base_url).rstrip("/")
    return f"{base_url}/verify-email?token={quote(token)}"


def send_verification_email(request: Request, user: User) -> EmailDeliveryResult:
    token = user.email_verification_token or ""
    verification_url = build_verification_url(request, token)
    settings = get_settings()

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

    if not settings.smtp_host or not settings.smtp_from_address:
        logger.warning("SMTP is not configured. Email verification link for %s: %s", user.email, verification_url)
        return EmailDeliveryResult(delivered=False, verification_url=verification_url)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (
        formataddr((settings.smtp_from_name, settings.smtp_from_address))
        if settings.smtp_from_name
        else settings.smtp_from_address
    )
    message["To"] = user.email
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
    except Exception:
        logger.exception("Failed to send email verification to %s", user.email)
        return EmailDeliveryResult(delivered=False, verification_url=verification_url)

    return EmailDeliveryResult(delivered=True, verification_url=verification_url)
