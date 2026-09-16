"""What a cheque image says: its number, its amount, its date, who paid.

Two readers, cheapest first:

* the PDF's own text layer, when the scan has one — a remittance stub
  prints "Check No." and the amount in plain text;
* otherwise Claude on Bedrock looks at the image and answers in JSON.

Either way the answer is checked for shape before it is believed: a cheque
number is 5 to 11 digits, an amount has cents. Anything else is recorded as
unreadable, for a person, never guessed.
"""
import base64
import io
import json
import os
import re

from src.eob.parse import money, norm_text
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Which Claude reads the images. Overridable per host: the model catalogue
# on Bedrock moves, and the first vision run tells us what the account has.
VISION_MODEL_ID = os.environ.get('BEDROCK_VISION_MODEL_ID', 'anthropic.claude-3-sonnet-20240229-v1:0')

IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}
CHECK_NO_RE = re.compile(r'\b\d{5,11}\b')

PROMPT = """This is a scan of a paper check (cheque) or its remittance stub, mailed by a health insurer to a medical clinic.
Read it and answer with ONE JSON object and nothing else:
{"check_number": "<digits only, the check number printed on the check — usually top right, and again in the MICR line at the bottom between the ⑈ symbols>",
 "amount": "<the amount paid, as digits with cents, e.g. 1234.56>",
 "check_date": "<MM/DD/YYYY or empty>",
 "payer": "<who issued the check, e.g. Blue Shield of California, or empty>",
 "payee": "<who it is paid to, or empty>",
 "confidence": "<high|medium|low>"}
If a field cannot be read, leave it as an empty string. Do not invent digits."""


# ------------------------------------------------------------ the image
def image_payload(path):
    """(base64, media_type) for the image Bedrock will see. A PDF is rendered
    from its first page."""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_TYPES:
        with open(path, 'rb') as fh:
            return base64.b64encode(fh.read()).decode('ascii'), IMAGE_TYPES[ext]
    if ext == '.pdf':
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            img = pdf.pages[0].to_image(resolution=170).original
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
    m = re.search(r'(?:check|cheque|chk|eft)\s*(?:no\.?|number|#|num\.?)?\s*:?\s*(\d{5,11})\b', t, re.I)
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


def parse_model_json(text):
    """The JSON object in the model's reply, shape-checked. {} when unusable."""
    m = re.search(r'\{.*\}', str(text or ''), re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {k: norm_text(data.get(k, '')) for k in ('check_number', 'amount', 'check_date', 'payer', 'payee', 'confidence')}
    out['check_number'] = re.sub(r'\D', '', out['check_number'])
    if not re.fullmatch(r'\d{5,11}', out['check_number']):
        out['check_number'] = ''
    out['amount'] = money(out['amount']) if out['amount'] else ''
    return out


# ------------------------------------------------------------ the readers
def read_with_vision(aws_client, path, model_id=VISION_MODEL_ID):
    b64, media = image_payload(path)
    body = json.dumps({
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 400,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': b64}},
            {'type': 'text', 'text': PROMPT},
        ]}],
    })
    runtime = aws_client.session.client('bedrock-runtime')
    resp = runtime.invoke_model(modelId=model_id, contentType='application/json',
                                accept='application/json', body=body)
    reply = json.loads(resp['body'].read())
    text = ''.join(c.get('text', '') for c in reply.get('content', []) if c.get('type') == 'text')
    return parse_model_json(text), text


def read_check(aws_client, path, model_id=VISION_MODEL_ID):
    """{'check_number', 'amount', 'check_date', 'payer', 'payee', 'read_by',
    'confidence', 'problem'} for one file."""
    result = {'check_number': '', 'amount': '', 'check_date': '', 'payer': '', 'payee': '',
              'read_by': '', 'confidence': '', 'problem': ''}
    text = pdf_text(path)
    if text:
        got = parse_check_text(text)
        if got['check_number'] and got['amount']:
            result.update(got, read_by='pdf_text', confidence='high')
            return result
    try:
        got, raw = read_with_vision(aws_client, path, model_id)
    except Exception as e:
        result['problem'] = f'the image could not be read by the model: {str(e)[:160]}'
        logger.warning(f"  ⚠️ {os.path.basename(path)}: {result['problem']}")
        return result
    if not got:
        result['problem'] = f'the model did not answer in JSON: {raw[:120]!r}'
        return result
    result.update({k: got.get(k, '') for k in ('check_number', 'amount', 'check_date', 'payer', 'payee', 'confidence')},
                  read_by='vision')
    if not result['check_number']:
        result['problem'] = 'no check number could be read off the image'
    elif not result['amount']:
        result['problem'] = 'no amount could be read off the image'
    return result
