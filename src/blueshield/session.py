"""Signing in to the Blue Shield of California provider portal.

One login, shared by every bot that needs the portal: the submissions flow
(SympliSend hand-off), and the EOB bot (claim status + Check/EFT details). It
used to be written out inline inside `blueshield_submissions`, and a second,
older copy still lives inside the `test_blueshield_login` debug task. A third
copy for the EOB bot was the point of no return, so the working one was lifted
here verbatim — the body below is the `blueshield_submissions` block as it
stood, with the handler's bare `return`s turned into `return False`.

The sequence: cookie banner, persistent-session shortcut, username/password
typed at human pace, PingFederate SSO pass-throughs, e-mail MFA via the
practice Gmail, and the "trust this device" interstitial.
"""
import random
import time
from urllib.parse import urlparse

from src.utils.logger import get_logger

logger = get_logger(__name__)

CLAIM_STATUS_URL = "https://www.blueshieldca.com/providerwebapp/claims/claimStatus"


def login_to_provider_portal(page, aws_client) -> bool:
    """Leave `page` inside the portal (a `providerwebapp` path), or return False.

    Never raises for a login failure; the caller decides whether to abort.
    """
    creds = aws_client.get_secret("blueshield_credentials")
    gmail_creds = aws_client.get_secret("gmail_credentials")

    # ─── Login to Blue Shield Portal ───
    target_url = "https://www.blueshieldca.com/providerwebapp/claims/claimStatus"
    logger.info(f"Navigating to: {target_url}")
    page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_load_state('networkidle', timeout=20000)
    except Exception:
        pass
    time.sleep(1)

    # Dismiss cookie consent
    try:
        cookie_btn = page.query_selector('button:has-text("Continue"), button:has-text("Accept"), button:has-text("OK")')
        if cookie_btn and cookie_btn.is_visible():
            cookie_btn.click()
            time.sleep(1)
    except Exception:
        pass

    from urllib.parse import urlparse
    current_url = page.url
    parsed = urlparse(current_url)
    is_on_portal = 'providerwebapp' in parsed.path

    if is_on_portal:
        logger.info("🎉 Already logged in via persistent session!")
    else:
        logger.info(f"Login required. Page: {page.title()}")

        # Wait for login form
        try:
            page.wait_for_selector(
                'input[name="pf.username"], input#username, '
                'input[placeholder="Username"], input[placeholder="Password"]',
                state='visible', timeout=30000
            )
        except Exception:
            pass
        time.sleep(random.uniform(1.0, 2.0))

        # Dismiss cookie banner again
        try:
            cookie_btn = page.query_selector('button:has-text("Continue"), button:has-text("Accept")')
            if cookie_btn and cookie_btn.is_visible():
                cookie_btn.click()
                time.sleep(1)
        except Exception:
            pass

        # Enter username
        username_field = (
            page.query_selector('input[name="pf.username"]') or
            page.query_selector('input#username') or
            page.query_selector('input[placeholder="Username"]')
        )
        if username_field:
            username_field.click()
            time.sleep(0.2)
            page.keyboard.press('Control+a')
            page.keyboard.press('Backspace')
            time.sleep(random.uniform(0.3, 0.8))
            for char in creds['username']:
                page.keyboard.type(char, delay=random.uniform(40, 120))
            logger.info("✅ Username entered")
        else:
            logger.error("❌ Username field not found")

        time.sleep(random.uniform(0.5, 1.2))

        # Enter password
        password_field = (
            page.query_selector('input[type="password"]') or
            page.query_selector('input[name="pf.pass"]')
        )
        if password_field:
            password_field.click()
            time.sleep(0.2)
            page.keyboard.press('Control+a')
            page.keyboard.press('Backspace')
            time.sleep(random.uniform(0.3, 0.7))
            for char in creds['password']:
                page.keyboard.type(char, delay=random.uniform(40, 100))
            logger.info("✅ Password entered")
        else:
            logger.error("❌ Password field not found")

        time.sleep(random.uniform(0.8, 2.0))

        # Submit login
        try:
            page.click('button:has-text("Log in")', timeout=3000)
            logger.info("✅ Clicked 'Log in'")
        except Exception:
            try:
                page.click('button:has-text("Sign in"), input[type="submit"]', timeout=3000)
                logger.info("✅ Clicked Sign In")
            except Exception:
                page.evaluate('''() => {
                    const form = document.querySelector('form');
                    if (form) {
                        const okField = document.querySelector('input[name="pf.ok"]');
                        if (okField) okField.value = "clicked";
                        form.submit();
                    }
                }''')
                logger.info("✅ Form submitted (fallback)")
        time.sleep(random.uniform(2, 4))

        # Handle SSO redirects
        for sso_attempt in range(8):
            current_url = page.url
            if 'providerwebapp' in urlparse(current_url).path:
                logger.info("🎉 Reached portal after SSO!")
                break
            page_text = page.inner_text('body').lower()
            if '2-step' in page_text or 'authentication method' in page_text or 'email at' in page_text:
                logger.info("🔐 MFA page detected")
                break

            has_login_form = page.query_selector('input[name="pf.username"], input#username')
            if has_login_form and has_login_form.is_visible():
                try:
                    page.click('button:has-text("Sign in")', timeout=3000)
                except Exception:
                    page.keyboard.press('Enter')
                time.sleep(random.uniform(2, 4))
                continue

            if page.title() == 'Sign On' or 'authorization.ping' in current_url:
                page.evaluate('''() => {
                    const form = document.querySelector('form');
                    if (form) {
                        const okField = document.querySelector('input[name="pf.ok"]');
                        if (okField) okField.value = "clicked";
                        form.submit();
                    }
                }''')
                time.sleep(random.uniform(2, 4))
                continue
            break

        # Handle MFA if needed
        page_text = page.inner_text('body').lower()
        if '2-step' in page_text or 'authentication method' in page_text or 'email at' in page_text:
            logger.info("🔐 MFA required")
            time.sleep(random.uniform(1.0, 2.0))

            try:
                page.click('text=Email at', timeout=5000)
                logger.info("✅ Selected Email method")
            except Exception:
                radio = page.query_selector('input[type="radio"]')
                if radio:
                    radio.click()

            time.sleep(random.uniform(0.5, 1.5))

            from src.utils.gmail_mfa import clear_old_mfa_emails, fetch_mfa_code
            clear_old_mfa_emails(gmail_creds['email'], gmail_creds['app_password'])

            try:
                page.click('text=Send code', timeout=5000)
                logger.info("✅ Clicked 'Send code'")
            except Exception:
                page.evaluate('() => { const f = document.querySelector("form"); if (f) f.submit(); }')

            time.sleep(3)

            mfa_code = fetch_mfa_code(
                gmail_user=gmail_creds['email'],
                gmail_app_password=gmail_creds['app_password'],
                max_wait_seconds=90,
                poll_interval=3
            )

            if mfa_code:
                logger.info(f"✅ MFA code: {mfa_code}")
                code_field = page.query_selector(
                    'input[name="pf.challengeResponse"], '
                    'input[type="text"]:not([name="pf.username"])'
                )
                if code_field:
                    code_field.fill(mfa_code)
                    time.sleep(random.uniform(0.5, 1.0))

                    for btn_text in ['Confirm', 'Verify', 'Submit', 'Continue', 'Sign on', 'Next']:
                        try:
                            page.click(f'text={btn_text}', timeout=2000)
                            logger.info(f"✅ Clicked '{btn_text}'")
                            break
                        except Exception:
                            continue

                    time.sleep(4)

                    # Handle SSO redirects after MFA
                    for i in range(5):
                        if 'providerwebapp' in page.url:
                            break
                        if page.title() == 'Sign On':
                            page.evaluate('() => { const f = document.querySelector("form"); if (f) f.submit(); }')
                            time.sleep(3)
                        else:
                            break
            else:
                logger.error("❌ MFA code not received")
                return False
    # Handle intermediate pages (e.g. "Remember Me", "Trust this device")
    for _attempt in range(8):
        cur_url = page.url
        cur_parsed = urlparse(cur_url)

        if 'providerwebapp' in cur_parsed.path:
            logger.info("🎉 Reached portal!")
            break

        # Handle ping-ext / SSO pass-through pages
        if 'ping-ext' in cur_url or page.title() == 'Sign On':
            logger.info(f"Intermediate page: {page.title()} — auto-submitting")
            page.evaluate('''() => {
                const form = document.querySelector('form');
                if (form) {
                    const okField = document.querySelector('input[name="pf.ok"]');
                    if (okField) okField.value = "clicked";
                    form.submit();
                }
            }''')
            time.sleep(3)
            continue

        # Handle "Remember me" / "Trust device" / "Stay signed in" pages
        page_text = page.inner_text('body').lower()
        if any(kw in page_text for kw in ['remember', 'trust', 'stay signed', '30 days', 'keep me']):
            logger.info(f"🔐 Trust/Remember page detected: {page.title()}")
            # Try clicking Yes/Accept/Continue/Submit
            clicked = False
            for btn_text in ['Yes', 'Accept', 'Continue', 'OK', 'Submit', "Don't ask again"]:
                try:
                    page.click(f'text={btn_text}', timeout=2000)
                    logger.info(f"✅ Clicked '{btn_text}' on trust page")
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                # Fallback: submit any form
                page.evaluate('''() => {
                    const form = document.querySelector('form');
                    if (form) form.submit();
                }''')
                logger.info("✅ Auto-submitted trust page form")
            time.sleep(3)
            continue

        # Unknown page — log and try submitting form
        logger.info(f"Unknown intermediate page: {page.title()[:50]} | {cur_url[:80]}")
        try:
            page.evaluate('() => { const f = document.querySelector("form"); if (f) f.submit(); }')
        except Exception:
            pass
        time.sleep(3)

    # Verify we're logged in
    final_parsed = urlparse(page.url)
    if 'providerwebapp' not in final_parsed.path:
        # Try navigating directly
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        if 'providerwebapp' not in urlparse(page.url).path:
            logger.error("❌ Could not login to Blue Shield portal")
            return False

    logger.info("🎉 LOGIN SUCCESS — Blue Shield Portal")
    return True
