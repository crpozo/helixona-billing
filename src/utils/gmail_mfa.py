"""
Gmail MFA Code Fetcher
Reads Blue Shield 2-step verification codes from Gmail via IMAP.
Uses Google App Password (not OAuth) for simplicity.
"""
import imaplib
import email
import re
import time
from src.utils.logger import get_logger

logger = get_logger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
BS_SENDER = "WDHwebdesk2@blueshieldca.com"
MFA_SUBJECT = "Your one-time verification code"


def clear_old_mfa_emails(gmail_user: str, gmail_app_password: str):
    """
    Mark ALL existing BS verification emails as read.
    Call this BEFORE triggering "Send code" so any new email will be UNSEEN.
    """
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(gmail_user, gmail_app_password)
        mail.select("INBOX")

        status, msg_ids = mail.search(None, f'(FROM "{BS_SENDER}" SUBJECT "{MFA_SUBJECT}" UNSEEN)')
        if status == "OK" and msg_ids[0]:
            count = len(msg_ids[0].split())
            for msg_id in msg_ids[0].split():
                mail.store(msg_id, '+FLAGS', '\\Seen')
            logger.info(f"Cleared {count} old BS verification emails")
        else:
            logger.info("No old BS emails to clear")

        mail.logout()
    except Exception as e:
        logger.warning(f"Could not clear old emails: {e}")


def fetch_mfa_code(gmail_user: str, gmail_app_password: str,
                   max_wait_seconds: int = 90, poll_interval: int = 5) -> str:
    """
    Poll Gmail for a NEW (UNSEEN) Blue Shield MFA code.
    Call clear_old_mfa_emails() first, then trigger "Send code", then call this.
    """
    logger.info(f"Polling Gmail for fresh MFA code...")
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            mail.login(gmail_user, gmail_app_password)
            mail.select("INBOX")

            search_criteria = f'(FROM "{BS_SENDER}" SUBJECT "{MFA_SUBJECT}" UNSEEN)'
            status, msg_ids = mail.search(None, search_criteria)

            if status == "OK" and msg_ids[0]:
                ids = msg_ids[0].split()
                latest_id = ids[-1]

                status, msg_data = mail.fetch(latest_id, "(RFC822)")
                if status == "OK":
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    body = _get_email_body(msg)

                    code_match = re.search(r'\b(\d{6})\b', body)
                    if code_match:
                        code = code_match.group(1)
                        logger.info(f"✅ MFA code captured: {code}")
                        mail.store(latest_id, '+FLAGS', '\\Seen')
                        mail.logout()
                        return code

            mail.logout()

        except Exception as e:
            logger.warning(f"IMAP poll error: {e}")

        elapsed = int(time.time() - start_time)
        logger.info(f"No MFA code yet... ({elapsed}s / {max_wait_seconds}s)")
        time.sleep(poll_interval)

    logger.error(f"MFA code not received within {max_wait_seconds}s")
    return None


def _get_email_body(msg) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    return part.get_payload(decode=True).decode('utf-8', errors='ignore')
                except Exception:
                    pass
            elif content_type == "text/html":
                try:
                    html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    text = re.sub(r'<[^>]+>', ' ', html)
                    return text
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode('utf-8', errors='ignore')
        except Exception:
            pass
    return ""
