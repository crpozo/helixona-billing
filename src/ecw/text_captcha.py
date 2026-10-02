"""
eCW's own text captcha — "Enter Captcha text here" under a distorted image
("pe374") — on the login and the security-image re-authentication page.

Until 2026-10 the only challenge was Cloudflare Turnstile, solved through
2Captcha's turnstile method. The re-auth page now shows this image captcha
instead; the bot filled the password, found no Turnstile, pressed Log In and
stayed on the login page. This module reads the image off the page and asks
2Captcha's normal-captcha method (method=base64) for the text; the caller
fills it in, submits, and on a wrong answer refreshes ("Try another text")
and tries again.

Nothing here is specific to a bot: the same page serves all four roles.
"""
import base64
import re
import time

from src.utils.logger import get_logger

logger = get_logger(__name__)

INPUT_SEL = ('input[placeholder*="aptcha"], #captchaText, input[name*="aptcha" i], input[id*="aptcha" i], '
             'input[name*="Captcha"], input[id*="Captcha"]')
IMG_SEL = 'img[src*="aptcha" i], img[id*="aptcha" i], img[alt*="aptcha" i], img[class*="aptcha" i]'
REFRESH_SEL = 'text="Try another text", a:has-text("Try another"), [id*="refresh" i][class*="aptcha" i]'
TWO_CAPTCHA_IN = "https://2captcha.com/in.php"
TWO_CAPTCHA_RES = "https://2captcha.com/res.php"


def captcha_input(frame):
    """The text box for the captcha, or None when the page has no text captcha."""
    try:
        el = frame.query_selector(INPUT_SEL)
        return el if el and el.is_visible() else None
    except Exception:
        return None


def captcha_image(frame, inp=None):
    """The captcha image: by its name, else the image just above the text box."""
    try:
        img = frame.query_selector(IMG_SEL)
        if img and img.is_visible():
            return img
        if inp is not None:
            handle = frame.evaluate_handle('''(inp) => {
                // The nearest image above the input, in document order.
                const imgs = Array.from(document.images).filter(i => i.offsetParent !== null && i.naturalWidth > 60 && i.naturalHeight > 20);
                const r = inp.getBoundingClientRect();
                let best = null, dist = 1e9;
                for (const i of imgs) {
                    const b = i.getBoundingClientRect();
                    if (b.bottom <= r.top + 4 && r.top - b.bottom < dist) { dist = r.top - b.bottom; best = i; }
                }
                return best;
            }''', inp)
            el = handle.as_element() if handle else None
            return el
    except Exception:
        pass
    return None


def image_b64(img):
    """PNG bytes of the image as rendered, base64 — a screenshot of the element
    works whatever the src is (data:, same-origin JSP, cache-busted)."""
    return base64.b64encode(img.screenshot(type='png')).decode('ascii')


def ask_2captcha(req, key, b64, timeout_s=90):
    """The text 2Captcha reads in the image, or '' — and the task id, so a
    wrong answer can be reported back."""
    resp = req.post(TWO_CAPTCHA_IN, data={'key': key, 'method': 'base64', 'body': b64, 'json': 1,
                                          'min_len': 3, 'max_len': 8, 'language': 2}, timeout=20).json()
    if resp.get('status') != 1:
        logger.warning(f"2Captcha (text) submit failed: {resp}")
        return '', ''
    task_id = resp['request']
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        got = req.get(TWO_CAPTCHA_RES, params={'key': key, 'action': 'get', 'id': task_id, 'json': 1}, timeout=20).json()
        if got.get('status') == 1:
            return str(got['request']).strip(), task_id
        if got.get('request') != 'CAPCHA_NOT_READY':
            logger.warning(f"2Captcha (text) error: {got}")
            return '', task_id
    logger.warning("2Captcha (text) timed out")
    return '', task_id


def report_bad(req, key, task_id):
    """Tell 2Captcha the answer was wrong (no charge for it)."""
    if not task_id:
        return
    try:
        req.get(TWO_CAPTCHA_RES, params={'key': key, 'action': 'reportbad', 'id': task_id}, timeout=10)
    except Exception:
        pass


def refresh(frame):
    """Click "Try another text" so a fresh image is shown."""
    try:
        el = frame.query_selector(REFRESH_SEL)
        if el:
            el.click()
            time.sleep(1.5)
            return True
    except Exception:
        pass
    return False


def solve_text_captcha(frame, aws_client, req, where='login page'):
    """Fill the text captcha on `frame` when there is one. Returns
    ('filled', task_id) / ('none', '') when the page has no text captcha /
    ('failed', task_id)."""
    inp = captcha_input(frame)
    if inp is None:
        return 'none', ''
    img = captcha_image(frame, inp)
    if img is None:
        logger.warning(f"⚠️ text captcha on the {where}, but its image was not found")
        return 'failed', ''
    key = ''
    try:
        key = aws_client.get_secret("captcha_api_key").get('api_key', '')
    except Exception as e:
        logger.warning(f"captcha_api_key secret: {str(e)[:120]}")
    if not key:
        logger.warning("No 2Captcha API key configured — set 'captcha_api_key' secret")
        return 'failed', ''
    logger.info(f"🔤 Text captcha on the {where} — sending the image to 2Captcha...")
    text, task_id = ask_2captcha(req, key, image_b64(img))
    if not text:
        return 'failed', task_id
    try:
        inp.fill('')
        inp.type(text, delay=60)
    except Exception as e:
        logger.warning(f"could not type the captcha text: {e}")
        return 'failed', task_id
    logger.info(f"✅ Text captcha filled ({len(text)} chars)")
    return 'filled', task_id


PASSWORD_SEL = '#passwordField, input[type="password"]'
LOCKOUT_RX = re.compile(r'exceeded your login attempts|account (is )?locked|try again in \d+ minutes', re.I)


def page_error(frame):
    """The red message eCW shows after a failed attempt, if any."""
    try:
        return frame.evaluate('''() => {
            const bad = [];
            for (const el of document.querySelectorAll('div, span, p, label, li')) {
                if (el.children.length > 2) continue;
                const t = (el.innerText || '').trim();
                if (!t || t.length > 300) continue;
                const c = getComputedStyle(el).color;
                if (/exceeded|locked|invalid|incorrect|captcha|wrong|expired|not match/i.test(t) && /rgb\((2[0-9][0-9]|1[89][0-9]), ?([0-9]{1,2}), ?([0-9]{1,2})\)/.test(c)) bad.push(t);
            }
            return bad.join(' | ').slice(0, 300);
        }''') or ''
    except Exception:
        return ''


def refill_password(frame, password):
    """eCW empties the password box after a failed attempt; a retry with the
    captcha alone is another failed attempt, and five of those lock the
    account for 15 minutes."""
    if not password:
        return False
    try:
        box = frame.query_selector(PASSWORD_SEL)
        if box and not (box.input_value() or '').strip():
            box.fill(password)
            try:
                frame.evaluate('(pwd) => { const h = document.querySelector("#password"); if (h) h.value = pwd; }', password)
            except Exception:
                pass
            logger.info("✅ Re-entered the password (the page had cleared it)")
            return True
    except Exception as e:
        logger.warning(f"could not re-enter the password: {e}")
    return False


def submit_with_captcha(frame, page, aws_client, req, submit_selector, where='re-auth page', attempts=3, settle=(3, 5), password=None):
    """Solve the text captcha (if any), submit, and on a wrong answer refresh
    and try again — re-entering the password each time, since eCW clears it.
    Stops at once when eCW says the account is locked. Returns True when the
    page left the login flow."""
    import random
    key = ''
    for attempt in range(1, attempts + 1):
        msg = page_error(frame)
        if msg and LOCKOUT_RX.search(msg):
            logger.error(f"❌ eCW has locked the account: \"{msg[:160]}\" — no more attempts; wait 15 minutes before the next run")
            return False
        refill_password(frame, password)
        state, task_id = solve_text_captcha(frame, aws_client, req, where)
        if state == 'failed' and attempt < attempts:
            refresh(frame)
            continue
        time.sleep(0.5)
        try:
            btn = frame.query_selector(submit_selector)
        except Exception:
            btn = None
        if btn:
            btn.click()
            logger.info(f"✅ Clicked submit on {where}")
        else:
            frame.evaluate('(f => f && HTMLFormElement.prototype.submit.call(f))(document.querySelector("form"))')
            logger.info(f"✅ Submitted {where} form programmatically")
        time.sleep(random.uniform(*settle))
        url = (page.url or '').lower()
        if 'login' not in url and 'getpwdpage' not in url:
            return True
        if state != 'filled':
            return False
        # Still here with a captcha box: the text was wrong. Say so, get a new image.
        if not key:
            try:
                key = aws_client.get_secret("captcha_api_key").get('api_key', '')
            except Exception:
                pass
        try:
            frame = next((f for f in page.frames if f.query_selector(INPUT_SEL)), frame)
        except Exception:
            pass
        msg = page_error(frame)
        logger.warning(f"⚠️ {where}: still on the login page after captcha attempt {attempt}/{attempts}"
                       + (f' — the page says: "{msg[:160]}"' if msg else ''))
        if msg and LOCKOUT_RX.search(msg):
            logger.error("❌ eCW has locked the account for 15 minutes — stopping; the next run after that will log in again")
            return False
        report_bad(req, key, task_id)
        if captcha_input(frame) is None:
            return False
        refresh(frame)
    return False
