"""A 2-step verification code typed by a person, for when Gmail cannot supply it.

Blue Shield e-mails a code at every fresh login. The bots read it from the
practice Gmail over IMAP; when that fails — an expired app password, as on
2026-09-10 (`AUTHENTICATIONFAILED`) — the only alternative was to watch the
code die unused while the operator held it in their hand.

This is the hand-off: the dashboard writes the code to one DynamoDB item, and
the login, having given Gmail its chance, polls that item for a few minutes.
A code counts only if it was submitted AFTER the bot asked the portal for one
(so a stale code from last week is never typed in), and it is consumed on
read (so two bots cannot both use it).
"""
from datetime import datetime, timezone
import time

from src.utils.logger import get_logger

logger = get_logger(__name__)

TABLE = 'helixona-tasks'
RELAY_KEY = 'mfa_code'


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def post_manual_code(dynamodb, code: str) -> dict:
    """Store a freshly typed code. Digits only, 4–8 of them; anything else is
    refused so a stray paste can never reach the portal."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if not (4 <= len(code) <= 8):
        raise ValueError('an MFA code is 4 to 8 digits')
    item = {'task_id': RELAY_KEY, 'code': code, 'submitted_at': now_iso(), 'used': False}
    dynamodb.Table(TABLE).put_item(Item=item)
    return item


def wait_for_manual_code(dynamodb, requested_at: str, timeout_s: int = 240,
                         poll_s: int = 3, until=None) -> str:
    """Poll for a code submitted after `requested_at` (ISO UTC). Consumes it.

    Returns '' when the wait runs out, or as soon as `until()` returns True —
    the caller's sign that the code is no longer needed, e.g. a person typed
    it straight into the portal over the VNC. Never raises: a table hiccup
    here must read as "no code", not as a crashed login.
    """
    deadline = time.time() + timeout_s
    announced = False
    while time.time() < deadline:
        try:
            if until is not None and until():
                logger.info("  the 2-step screen is gone — someone completed it on screen; not waiting for a code")
                return ''
        except Exception:
            pass
        try:
            table = dynamodb.Table(TABLE)
            item = table.get_item(Key={'task_id': RELAY_KEY}).get('Item') or {}
            fresh = (str(item.get('submitted_at', '')) > requested_at
                     and not item.get('used'))
            if fresh and item.get('code'):
                table.update_item(
                    Key={'task_id': RELAY_KEY},
                    UpdateExpression='SET used = :t',
                    ExpressionAttributeValues={':t': True},
                )
                logger.info("✅ MFA code received from the dashboard")
                return str(item['code'])
        except Exception as e:
            logger.warning(f"MFA relay read failed: {e}")
        if not announced:
            announced = True
        remaining = int(deadline - time.time())
        if remaining % 30 < poll_s:
            logger.info(f"  ⌛ still waiting for a code from the dashboard ({remaining}s left)")
        time.sleep(poll_s)
    return ''
