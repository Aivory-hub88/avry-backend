"""
Email a person who was just added to a workspace.

The dashboard adds the member row (dashboard.workspace_members) itself; this
only tells them. Nothing is trusted from the caller beyond the ids: the
recipient and workspace name come from the database, and the caller must be
the one who added that member, so the endpoint can't be used to mail arbitrary
addresses with arbitrary text.
"""

import asyncio
import html as html_lib
import logging
import os
from typing import Optional

from app.config import settings
from app.services import email_service

logger = logging.getLogger(__name__)

ROLE_LABELS = {
    "owner": "an admin",
    "editor": "an editor",
    "viewer": "a viewer",
}


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(dsn, connect_timeout=5)


def fetch_invite_facts(workspace_id: str, user_id: str) -> Optional[dict]:
    """Member row + recipient + inviter, or None when there is no such member."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.role, m.added_by, w.name, u.email, u.full_name,"
                "       a.full_name, a.email"
                " FROM dashboard.workspace_members m"
                " JOIN dashboard.workspaces w ON w.id = m.workspace_id"
                " JOIN identity.users u ON u.id::text = m.user_id::text"
                " LEFT JOIN identity.users a ON a.id::text = m.added_by::text"
                " WHERE m.workspace_id = %s AND m.user_id::text = %s",
                (workspace_id, user_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "role": row[0],
        "added_by": str(row[1]) if row[1] is not None else None,
        "workspace_name": row[2] or workspace_id,
        "email": row[3],
        "full_name": row[4],
        "inviter_name": row[5] or row[6] or "A teammate",
    }


def workspace_link() -> str:
    return settings.workspace_url_base


def build_message(*, name: str, inviter: str, workspace: str, role: str, link: str):
    """(subject, html, text)."""
    role_label = ROLE_LABELS.get(role, "a member")
    subject = f"{inviter} added you to {workspace} on Aivory"
    text = (
        f"Hi {name},\n\n"
        f"{inviter} added you to the workspace \"{workspace}\" on Aivory as "
        f"{role_label}.\n\nOpen it here:\n{link}\n\n- Aivory"
    )
    e = html_lib.escape
    html = (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#0f1310;'
        'font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8ece4">'
        '<div style="max-width:520px;margin:0 auto;background:#161b16;'
        'border:1px solid #2a322a;border-radius:12px;padding:32px">'
        '<h1 style="margin:0 0 16px;font-size:20px;font-weight:600;color:#ffffff">'
        "You've been added to a workspace</h1>"
        '<p style="margin:0 0 12px;font-size:14px;line-height:1.6;color:#c3cbbc">'
        f"Hi {e(name)},</p>"
        '<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:#c3cbbc">'
        f"<strong>{e(inviter)}</strong> added you to <strong>{e(workspace)}</strong> "
        f"on Aivory as {e(role_label)}.</p>"
        '<p style="margin:0 0 24px">'
        f'<a href="{e(link, quote=True)}" style="display:inline-block;background:#b2cca2;'
        'color:#0f1310;text-decoration:none;font-weight:600;font-size:14px;'
        'padding:12px 24px;border-radius:8px">Open workspace</a></p>'
        '<p style="margin:0;font-size:12px;line-height:1.6;color:#7d8878">'
        f"If the button does not work, paste this into your browser: {e(link)}</p>"
        "</div></body></html>"
    )
    return subject, html, text


async def notify_member(workspace_id: str, user_id: str, caller_id: str) -> str:
    """
    Returns "sent", "not_member", "forbidden" or "failed". Never raises on a
    mail problem: the member is already added, the email is best effort.
    """
    facts = await asyncio.to_thread(fetch_invite_facts, workspace_id, user_id)
    if not facts:
        return "not_member"
    if facts["added_by"] != caller_id:
        return "forbidden"
    if not facts["email"]:
        return "failed"
    subject, html, text = build_message(
        name=facts["full_name"] or "there",
        inviter=facts["inviter_name"],
        workspace=facts["workspace_name"],
        role=facts["role"],
        link=workspace_link(),
    )
    sent = await email_service.send_email(
        facts["email"], subject, html, text,
        from_address=settings.notification_from_email,
    )
    return "sent" if sent else "failed"
