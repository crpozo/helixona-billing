"""What a check image says: its number, its amount, its date, who paid.

Two readers, cheapest first:

* the PDF's own text layer, when the scan has one — a remittance stub
  prints "Check No." and the amount in plain text;
* otherwise Claude (the Anthropic API) looks at the image and answers in JSON.

Either way the answer is checked for shape before it is believed: a check
number is 5 to 11 digits, an amount has cents. Anything else is recorded as
unreadable, for a person, never guessed.

A scan is not always one check. The department also files the bank deposit:
page 1 is the deposit slip (the checks listed by hand, number and amount,
and the total), and each page after it is one of those checks. Such a file
is read page by page and comes back as a deposit — the checks in it, each
its own case, and the total (2026-09-18: 04222026_001.pdf, 29 checks,
$11,099.18; the one-check reader had made a "check 1100411" of the slip).
"""
import base64
import io
import json
import os
import re

from src.eob.parse import money, norm_text
from src.utils.logger import get_logger

logger = get_logger(__name__)

# The Claude API, directly (not Bedrock — 2026-09-17). The key comes from
# ANTHROPIC_API_KEY, else the Secrets Manager secret
# helixona-prod-anthropic-api-key (the one provisioned with the account:
# the bare key, or JSON with api_key / ANTHROPIC_API_KEY), else
# prod/helixona/anthropic_credentials {"api_key": ...}. Model: VISION_MODEL_ID.
KEY_SECRETS = [os.environ.get('ANTHROPIC_SECRET_NAME', 'prod/helixona/anthropic_credentials'),
               'helixona-prod-anthropic-api-key']  # the provisioned one, us-east-1, only if the first is missing
# The provisioned secret lives in us-east-1 (the bot runs in us-west-2), so
# each name is tried in the bot's region and then in these.
KEY_SECRET_REGIONS = [r for r in os.environ.get('ANTHROPIC_SECRET_REGIONS', 'us-east-1').split(',') if r.strip()]
VISION_MODEL_ID = os.environ.get('VISION_MODEL_ID', 'claude-sonnet-5')  # the operator's choice, 2026-09-17
_CLIENT = {}

IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}
CHECK_NO_RE = re.compile(r'\b\d{5,11}\b')

PROMPT = """This is one page of a scan filed by a medical clinic's billing department. It is ONE of:
 (a) a paper check (or its remittance stub) mailed by a health insurer or a patient;
 (b) a bank DEPOSIT SLIP / deposit ticket — a form listing several checks by hand (a numbered list "CHECKS (List each separately)", each line a check number and its dollars and cents) with a total;
 (c) something else (a letter, an EOB page, a blank or back side).
Answer with ONE JSON object and nothing else:
{"kind": "<check|deposit_slip|other>",
 "check": {"check_number": "<digits only, the check number printed on the check — usually top right, and again in the MICR line at the bottom between the ⑈ symbols; keep leading zeros>",
           "amount": "<the amount paid, as digits with cents, e.g. 1234.56>",
           "check_date": "<MM/DD/YYYY or empty>",
           "payer": "<who issued the check, e.g. Blue Shield of California, or empty>",
           "payee": "<who it is paid to, or empty>"},
 "deposit": {"date": "<MM/DD/YYYY or empty>",
             "total": "<the deposit total as digits with cents, or empty>",
             "checks": [{"check_number": "<digits as written>", "amount": "<digits with cents>"}]},
 "confidence": "<high|medium|low>"}
Fill "check" only for kind check and "deposit" only for kind deposit_slip; leave the other empty. Transcribe every line of a deposit slip, in order.
If a field cannot be read, leave it as an empty string. Do not invent digits."""
DEPOSIT_MAX_PAGES = 80   # one model call per page of a deposit


# ------------------------------------------------------------ the image
def pdf_page_count(path):
    if not path.lower().endswith('.pdf'):
        return 1
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 1


def image_payload(path, page_no=0):
    """(base64, media_type) for the image the model will see. A PDF is
    rendered from page `page_no` (0-based) — the check number of a
    multi-page EOB is often not on the first page."""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_TYPES:
        with open(path, 'rb') as fh:
            return base64.b64encode(fh.read()).decode('ascii'), IMAGE_TYPES[ext]
    if ext == '.pdf':
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            page_no = max(0, min(page_no, len(pdf.pages) - 1))
            img = pdf.pages[page_no].to_image(resolution=170).original
        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode('ascii'), 'image/png'
    # TIFF, HEIC and the rest: PIL to PNG when it can.
    from PIL import Image
    img = Image.open(path)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii'), 'image/png'


def pdf_text(path):
    if not path.lower().endswith('.pdf'):
        return ''
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return '\n'.join((p.extract_text() or '') for p in pdf.pages[:2])
    except Exception as e:
        logger.warning(f"  could not read PDF text: {e}")
        return ''


# ------------------------------------------------------------ the parsing
def parse_check_text(text):
    """Number and amount from a stub's text layer. Only when both are named
    on the page ("Check No", "Amount"): a bare number could be anything."""
    t = norm_text(text)
    out = {'check_number': '', 'amount': '', 'check_date': '', 'payer': '', 'payee': ''}
    m = re.search(r'(?:check|check|chk|eft)\s*(?:no\.?|number|#|num\.?)?\s*:?\s*(\d{5,11})\b', t, re.I)
    if m:
        out['check_number'] = m.group(1)
    m = re.search(r'(?:amount|pay|total|net)[^$\d]{0,30}\$?\s*([\d,]+\.\d{2})', t, re.I)
    if m:
        out['amount'] = money(m.group(1))
    m = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', t)
    if m:
        out['check_date'] = m.group(1)
    if re.search(r'blue\s*shield', t, re.I):
        out['payer'] = 'Blue Shield of California'
    return out


def _check_fields(data, confidence=''):
    out = {k: norm_text((data or {}).get(k, '')) for k in ('check_number', 'amount', 'check_date', 'payer', 'payee')}
    out['confidence'] = norm_text(confidence or (data or {}).get('confidence', ''))
    out['check_number'] = re.sub(r'\D', '', out['check_number'])
    if not re.fullmatch(r'\d{5,11}', out['check_number']):
        out['check_number'] = ''
    out['amount'] = money(out['amount']) if out['amount'] else ''
    return out


def parse_model_json(text):
    """The JSON object in the model's reply, shape-checked: one check's
    fields, from the page shape ("kind"/"check") or the flat one. {} when
    unusable."""
    page = parse_page_json(text)
    if not page:
        return {}
    return page['check']


def parse_page_json(text):
    """{'kind': check|deposit_slip|other, 'check': {...}, 'deposit': {'date',
    'total', 'checks': [{'check_number', 'amount'}]}, 'confidence'}, or {}
    when the reply is not JSON. A flat reply (the fields at the top) is a
    check. A deposit slip needs at least one legible line."""
    m = re.search(r'\{.*\}', str(text or ''), re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    confidence = norm_text(data.get('confidence', ''))
    kind = norm_text(data.get('kind', '')).lower()
    if kind not in ('check', 'deposit_slip', 'other'):
        kind = 'check' if 'check_number' in data else 'other'
    check = _check_fields(data.get('check') if isinstance(data.get('check'), dict) else data, confidence)
    dep_in = data.get('deposit') if isinstance(data.get('deposit'), dict) else {}
    lines = []
    for ln in (dep_in.get('checks') or []):
        if not isinstance(ln, dict):
            continue
        num = re.sub(r'\D', '', norm_text(ln.get('check_number', '')))
        amt = money(norm_text(ln.get('amount', ''))) if ln.get('amount') else ''
        if re.fullmatch(r'\d{4,12}', num):
            lines.append({'check_number': num, 'amount': amt})
    deposit = {'date': norm_text(dep_in.get('date', '')),
               'total': money(norm_text(dep_in.get('total', ''))) if dep_in.get('total') else '',
               'checks': lines}
    if kind == 'deposit_slip' and not lines and not deposit['total']:
        kind = 'other'
    if kind == 'check' and not check['check_number'] and lines:
        kind = 'deposit_slip'
    return {'kind': kind, 'check': check if kind == 'check' else _check_fields({}), 'deposit': deposit, 'confidence': confidence}


# ------------------------------------------------------------ the readers
def _secret_clients(aws_client):
    """The bot's own Secrets Manager client, then one per extra region."""
    yield '', aws_client.secrets
    for region in KEY_SECRET_REGIONS:
        try:
            yield region, aws_client.session.client('secretsmanager', region_name=region.strip())
        except Exception:
            continue


def _key_from_secret(aws_client, name):
    """The key in a secret: the bare string, or JSON under api_key /
    ANTHROPIC_API_KEY / key. Looked for in the bot's region, then the others."""
    raw = ''
    for region, client in _secret_clients(aws_client):
        try:
            raw = client.get_secret_value(SecretId=name).get('SecretString', '') or ''
            if raw:
                break
        except Exception as e:
            msg = str(e)
            if 'AccessDenied' in msg or 'KMS' in msg or 'kms' in msg:
                logger.error(f"  ❌ secret {name}{' in ' + region if region else ''}: the bot may not decrypt it — "
                             f"give the role helixona-agent-role kms:Decrypt on the key the secret uses "
                             f"(helixona-prod-phi-data), or store the key in a secret with the default AWS key: {msg[:120]}")
            else:
                logger.info(f"  (secret {name}{' in ' + region if region else ''}: {msg[:100]})")
    raw = raw.strip()
    if raw.startswith('{'):
        try:
            data = json.loads(raw)
        except ValueError:
            return ''
        for k in ('api_key', 'ANTHROPIC_API_KEY', 'anthropic_api_key', 'key'):
            if data.get(k):
                return str(data[k]).strip()
        return ''
    return raw


def _client(aws_client):
    """One Anthropic client per process. The key: ANTHROPIC_API_KEY, else
    the first of KEY_SECRETS that holds one."""
    if 'client' not in _CLIENT:
        import anthropic
        key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
        for name in KEY_SECRETS:
            if key:
                break
            key = _key_from_secret(aws_client, name)
            if key:
                logger.info(f"  🔑 Anthropic API key from secret {name}")
        if not key.startswith('sk-ant-'):
            raise RuntimeError('no Anthropic API key: set ANTHROPIC_API_KEY or fill the secret '
                               f'{KEY_SECRETS[0]} with the key (sk-ant-...)')
        _CLIENT['client'] = anthropic.Anthropic(api_key=key)
    return _CLIENT['client']


def read_with_vision(aws_client, path, model_id=VISION_MODEL_ID, page_no=0):
    """Claude looks at the image and answers the JSON in PROMPT."""
    b64, media = image_payload(path, page_no)
    client = _client(aws_client)
    response = client.beta.messages.create(
        model=model_id,
        max_tokens=1024,
        output_config={'effort': 'low'},
        betas=['server-side-fallback-2026-07-01'],
        fallbacks='default',
        messages=[{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': b64}},
            {'type': 'text', 'text': PROMPT},
        ]}],
    )
    if response.stop_reason == 'refusal':
        raise RuntimeError('the model declined to read this image')
    text = ''.join(getattr(b, 'text', '') for b in response.content if getattr(b, 'type', '') == 'text')
    return parse_model_json(text), text


def read_page(aws_client, path, page_no, model_id=VISION_MODEL_ID):
    """One page as the model sees it: parse_page_json's shape (or {}), and
    the raw reply."""
    b64, media = image_payload(path, page_no)
    client = _client(aws_client)
    response = client.beta.messages.create(
        model=model_id,
        max_tokens=2048,
        output_config={'effort': 'low'},
        betas=['server-side-fallback-2026-07-01'],
        fallbacks='default',
        messages=[{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': b64}},
            {'type': 'text', 'text': PROMPT},
        ]}],
    )
    if response.stop_reason == 'refusal':
        raise RuntimeError('the model declined to read this image')
    text = ''.join(getattr(b, 'text', '') for b in response.content if getattr(b, 'type', '') == 'text')
    return parse_page_json(text), text


def read_check(aws_client, path, model_id=VISION_MODEL_ID):
    """One file. A check: {'kind': 'check', 'check_number', 'amount',
    'check_date', 'payer', 'payee', 'read_by', 'confidence', 'problem',
    'pages'}. A deposit: {'kind': 'deposit', 'deposit_date', 'deposit_total',
    'checks': [{'check_number', 'amount', 'check_date', 'payer', 'payee',
    'page', 'from_slip'}], 'pages', 'read_by', 'problem'}. A failure names
    its 'error_kind': 'file' (the file's fault) or 'api' (the account's)."""
    result = {'kind': 'check', 'check_number': '', 'amount': '', 'check_date': '', 'payer': '', 'payee': '',
              'read_by': '', 'confidence': '', 'problem': ''}
    n = pdf_page_count(path)
    result['pages'] = n
    text = pdf_text(path)
    if text:
        got = parse_check_text(text)
        if got['check_number'] and got['amount']:
            result.update(got, read_by='pdf_text', confidence='high')
            return result
    # Page 1, then page 2 and the last page of a longer PDF: a multi-page
    # EOB carries the check on a later page (2026-09-18: forty Blue Shield
    # EOBs gave "no check number" off their cover page). A deposit slip on
    # page 1 means every page is read.
    pages = [0]
    if n > 1:
        pages += [p for p in (1, n - 1) if p not in pages]
    got, raw, tried = {}, '', []
    for page_no in pages[:3]:
        page, raw, err = _read_one(aws_client, path, page_no, model_id)
        if err:
            result.update(err)
            return result
        tried.append(page_no + 1)
        if page.get('kind') == 'deposit_slip' and page_no == 0:
            return _read_deposit(aws_client, path, page, n, model_id, result)
        got = page.get('check') or {}
        if got.get('check_number'):
            if page_no:
                logger.info(f"  (check number found on page {page_no + 1} of {n})")
            break
    if not got:
        result['problem'] = f'the model did not answer in JSON: {raw[:120]!r}'
        return result
    result['pages_tried'] = tried
    result.update({k: got.get(k, '') for k in ('check_number', 'amount', 'check_date', 'payer', 'payee', 'confidence')},
                  read_by='vision')
    if not result['check_number']:
        result['problem'] = f"no check number could be read off the image (page{'s' if len(tried) > 1 else ''} {', '.join(map(str, tried))} of {n})"
    elif not result['amount']:
        result['problem'] = 'no amount could be read off the image'
    return result


def _read_one(aws_client, path, page_no, model_id):
    """(page, raw, error): the page as parse_page_json gives it, or an
    error dict naming whose fault it is."""
    try:
        image_payload(path, page_no)   # the file itself: a render failure is the file's
    except Exception as e:
        problem = f'the file could not be rendered: {str(e)[:160]}'
        logger.warning(f"  ⚠️ {os.path.basename(path)}: {problem}")
        return {}, '', {'problem': problem, 'error_kind': 'file'}
    try:
        page, raw = read_page(aws_client, path, page_no, model_id)
    except Exception as e:
        # The API refused (no credit, bad key, rate limit, outage): the
        # account's problem, not the file's — the caller must not count
        # it against the file, and should stop rather than fail 700 times.
        problem = f'the Anthropic API did not answer: {str(e)[:200]}'
        logger.warning(f"  ⚠️ {os.path.basename(path)}: {problem}")
        return {}, '', {'problem': problem, 'error_kind': 'api'}
    return page or {}, raw, None


def _read_deposit(aws_client, path, slip, n, model_id, result):
    """A deposit: the slip's list (page 1), then each page after it — the
    checks as printed win over the hand-written list; a line of the slip
    with no page of its own is kept from the slip and marked so."""
    dep = slip.get('deposit') or {}
    listed = dep.get('checks') or []
    logger.info(f"  🏦 deposit slip: {len(listed)} check(s) listed · total ${dep.get('total') or '?'} · {n} page(s)")
    found = []
    for page_no in range(1, min(n, DEPOSIT_MAX_PAGES)):
        page, raw, err = _read_one(aws_client, path, page_no, model_id)
        if err:
            if err.get('error_kind') == 'api':
                result.update(err)
                return result
            continue
        ck = (page.get('check') or {}) if page.get('kind') == 'check' else {}
        if ck.get('check_number'):
            found.append({**{k: ck.get(k, '') for k in ('check_number', 'amount', 'check_date', 'payer', 'payee')},
                          'page': page_no + 1, 'from_slip': False})
    bare = lambda x: str(x or '').lstrip('0')
    seen = {bare(c['check_number']) for c in found}
    for ln in listed:
        if bare(ln['check_number']) not in seen:
            seen.add(bare(ln['check_number']))
            found.append({'check_number': ln['check_number'], 'amount': ln.get('amount', ''), 'check_date': '',
                          'payer': '', 'payee': '', 'page': 1, 'from_slip': True})
    total = dep.get('total') or ''
    if not total and found and all(c.get('amount') for c in found):
        total = money(str(sum(float(c['amount']) for c in found)))
    result.update(kind='deposit', deposit_date=dep.get('date', ''), deposit_total=total, checks=found,
                  read_by='vision', confidence=slip.get('confidence', ''),
                  problem='' if found else 'a deposit slip with no legible check')
    from_slip = sum(1 for c in found if c['from_slip'])
    logger.info(f"  🏦 deposit of {len(found)} check(s)" + (f" ({from_slip} only on the slip)" if from_slip else '')
                + f" · total ${total or '?'}")
    return result
