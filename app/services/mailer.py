"""SMTP email notifications (DB driven)."""

from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from sqlalchemy.orm import Session

from ..settings_service import as_bool, as_int, get_setting


def _split_emails(value: str) -> list[str]:
    return [item.strip() for item in (value or "").replace(";", ",").split(",") if item.strip()]


def smtp_configured(db: Session) -> bool:
    return bool(get_setting(db, "smtp.host") and get_setting(db, "smtp.username") and get_setting(db, "smtp.password"))


def send_email(
    db: Session,
    recipients: list[str] | str,
    subject: str,
    body: str,
    cc: list[str] | None = None,
    html: str | None = None,
) -> tuple[bool, str]:
    if not as_bool(get_setting(db, "smtp.enabled"), True):
        return False, "SMTP is disabled"
    if not smtp_configured(db):
        return False, "SMTP credentials are incomplete"
    if isinstance(recipients, str):
        recipients = _split_emails(recipients)
    recipients = [email for email in recipients if email]
    if not recipients:
        return False, "No recipient email supplied"

    host = get_setting(db, "smtp.host")
    port = as_int(get_setting(db, "smtp.port"), 587)
    username = get_setting(db, "smtp.username")
    password = get_setting(db, "smtp.password")
    from_email = get_setting(db, "smtp.from_email") or username
    from_name = get_setting(db, "smtp.from_name") or "CCTV"
    cc_all = cc if cc is not None else _split_emails(get_setting(db, "smtp.cc_emails"))

    message = MIMEMultipart("alternative")
    message["From"] = formataddr((from_name, from_email))
    message["To"] = ", ".join(recipients)
    if cc_all:
        message["Cc"] = ", ".join(cc_all)
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))
    if html:
        message.attach(MIMEText(html, "html", "utf-8"))

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=25, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=25)
            server.ehlo()
            try:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            except smtplib.SMTPException:
                pass
        server.login(username, password)
        server.sendmail(from_email, recipients + cc_all, message.as_string())
        server.quit()
        return True, f"Email sent to {len(recipients) + len(cc_all)} recipient(s)"
    except Exception as exc:
        return False, str(exc)


def send_footage_email(
    db: Session,
    recipient: str,
    drive_link: str,
    request_data: dict[str, Any],
    *,
    approver: bool = False,
) -> tuple[bool, str]:
    approver_email = get_setting(db, "smtp.approver_email")
    name_part = recipient.split("@")[0]
    display_name = name_part.replace(".", " ").replace("_", " ").title()
    incident = request_data.get("incident") or "CCTV Request"
    campus = request_data.get("campus") or "Campus"
    date = request_data.get("date") or ""

    if approver or (approver_email and recipient.lower() == approver_email.lower()):
        subject = f"Approval Required: CCTV Footage - {incident}"
        body = f"""Hi {display_name},

A CCTV footage folder has been prepared for the incident: {incident}.

Requested by: {request_data.get('requester') or request_data.get('requester_email') or 'Unknown'}
Location/Campus: {campus}
Date of incident: {date}

Please review the footage here:
{drive_link}

Let me know if I have your approval to share this with the requester.

Thank you,
Footprints IT Group
"""
    else:
        subject = f"Requested CCTV Footage: {campus} - {date}"
        body = f"""Hi {display_name},

Here is the CCTV footage you requested regarding the incident at {campus} on {date}.

Link to footage:
{drive_link}

You have been granted temporary Viewer access. For security purposes, this access will automatically expire in {request_data.get('expiry_days', 30)} days.
Please allow 5-10 minutes for Google to finish processing the videos.

Thank you,
Footprints IT Group
"""
    return send_email(db, recipient, subject, body)


def send_test_email(db: Session, recipient: str) -> tuple[bool, str]:
    return send_email(
        db,
        recipient,
        "CCTV Control Center - SMTP test",
        "This is a test email from your CCTV V3 control center. SMTP is working.",
    )
