"""
Claude API spend, tallied from every response the bots get back.

The Anthropic console shows the organization's balance, but reading it from
code needs an Admin API key the clinic does not have. What every call does
return is `response.usage` — the tokens it consumed — and `response.model`,
the model that actually answered (the vision reader asks for one model and
lets the API fall back to another). Priced at the list rates below and added
to one DynamoDB table, that is a running meter: spent today, this month, all
time. The super admin enters the console balance once; from then on the
dashboard shows what is left as balance − spend since.

Fail-soft on purpose: a tally that cannot be written must never fail the
read it was tallying.
"""
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.utils.logger import get_logger

logger = get_logger(__name__)

USAGE_TABLE = os.environ.get('USAGE_TABLE', 'helixona-usage')

# $ per million tokens: input, output, cache write (1.25× input), cache read.
# Longest prefix wins. List prices of 2026-09; a new model falls back to the
# vision reader's own (Sonnet 5) so a tally is never silently zero.
PRICES = [
    ('claude-fable-5-1', 10.00, 50.00, 12.50, 0.25),
    ('claude-fable-5', 10.00, 50.00, 12.50, 1.00),
    ('claude-opus-5-5', 4.00, 20.00, 5.00, 0.20),
    ('claude-opus-5', 5.00, 25.00, 6.25, 0.50),
    ('claude-opus-4', 5.00, 25.00, 6.25, 0.50),
    ('claude-sonnet-5-5', 2.00, 10.00, 2.50, 0.20),
    ('claude-sonnet-5', 2.00, 10.00, 2.50, 0.20),
    ('claude-sonnet-4', 3.00, 15.00, 3.75, 0.30),
    ('claude-haiku-4-5', 1.00, 5.00, 1.25, 0.10),
]
DEFAULT_PRICE = ('claude-sonnet-5', 2.00, 10.00, 2.50, 0.20)


def price_for(model):
    """(input, output, cache_write, cache_read) in $ per MTok for a model id."""
    m = str(model or '').lower()
    best = None
    for row in PRICES:
        if m.startswith(row[0]) and (best is None or len(row[0]) > len(best[0])):
            best = row
    return (best or DEFAULT_PRICE)[1:]


def tokens_of(usage):
    """The four token counts off a response's usage object (or dict)."""
    def g(k):
        v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, 0)
        return int(v or 0)
    return {'input_tokens': g('input_tokens'), 'output_tokens': g('output_tokens'),
            'cache_write_tokens': g('cache_creation_input_tokens'), 'cache_read_tokens': g('cache_read_input_tokens')}


def cost_usd(model, usage):
    """What one response cost, in dollars (Decimal, 6 places)."""
    pin, pout, pw, pr = price_for(model)
    t = tokens_of(usage)
    dollars = (t['input_tokens'] * pin + t['output_tokens'] * pout
               + t['cache_write_tokens'] * pw + t['cache_read_tokens'] * pr) / 1_000_000
    return Decimal(str(round(dollars, 6)))


def ensure_table(ddb):
    if USAGE_TABLE in [t.name for t in ddb.tables.all()]:
        return ddb.Table(USAGE_TABLE)
    logger.info(f"🆕 creating table {USAGE_TABLE}")
    t = ddb.create_table(TableName=USAGE_TABLE,
                         KeySchema=[{'AttributeName': 'k', 'KeyType': 'HASH'}],
                         AttributeDefinitions=[{'AttributeName': 'k', 'AttributeType': 'S'}],
                         BillingMode='PAY_PER_REQUEST')
    t.wait_until_exists()
    return t


_TABLE = {}


def _table(aws_client):
    if 't' not in _TABLE:
        _TABLE['t'] = ensure_table(aws_client.dynamodb)
    return _TABLE['t']


def _model_attr(model):
    return 'cost_' + ''.join(ch if ch.isalnum() else '_' for ch in str(model or 'unknown').lower())


def record(aws_client, response, what=''):
    """Add one response to today's row and the all-time row. Never raises."""
    try:
        model = getattr(response, 'model', '') or ''
        usage = getattr(response, 'usage', None)
        if usage is None:
            return None
        t = tokens_of(usage)
        cost = cost_usd(model, usage)
        table = _table(aws_client)
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        for key in (f'day:{day}', '_total'):
            table.update_item(
                Key={'k': key},
                UpdateExpression=('ADD calls :one, input_tokens :i, output_tokens :o, cache_write_tokens :w, '
                                  'cache_read_tokens :r, cost_usd :c, #m :c SET last_at = :at, last_model = :model'),
                ExpressionAttributeNames={'#m': _model_attr(model)},
                ExpressionAttributeValues={':one': 1, ':i': t['input_tokens'], ':o': t['output_tokens'],
                                           ':w': t['cache_write_tokens'], ':r': t['cache_read_tokens'],
                                           ':c': cost, ':at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                           ':model': model})
        return cost
    except Exception as e:
        logger.warning(f"  (usage tally skipped: {str(e)[:120]})")
        return None


def set_credits(ddb, balance_usd, who=''):
    """The balance the Anthropic console shows today, entered by the super
    admin. Remaining = this − what the meter adds from now on."""
    table = ddb.Table(USAGE_TABLE)
    total = table.get_item(Key={'k': '_total'}).get('Item', {})
    item = {'k': '_credits', 'balance_usd': Decimal(str(round(float(balance_usd), 2))),
            'spent_before': total.get('cost_usd', Decimal('0')),
            'as_of': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'set_by': who}
    table.put_item(Item=item)
    return item


def _num(v):
    return float(v) if isinstance(v, (int, float, Decimal)) else 0.0


def read(ddb, today=None):
    """Everything the credits tile needs. A missing table reads as zero."""
    today = today or datetime.now(timezone.utc).date()
    month = today.strftime('%Y-%m')
    try:
        table = ddb.Table(USAGE_TABLE)
        from src.aws.clients import scan_all
        items = scan_all(table)
    except Exception as e:
        return {'ok': False, 'error': str(e)[:160], 'total': {}, 'days': [], 'credits': None}
    total = next((it for it in items if it.get('k') == '_total'), {})
    credits = next((it for it in items if it.get('k') == '_credits'), None)
    days = sorted((it for it in items if str(it.get('k', '')).startswith('day:')), key=lambda it: it['k'])

    def pack(it):
        models = {k[5:]: _num(v) for k, v in it.items() if k.startswith('cost_') and k != 'cost_usd'}
        return {'calls': int(_num(it.get('calls'))), 'input_tokens': int(_num(it.get('input_tokens'))),
                'output_tokens': int(_num(it.get('output_tokens'))), 'cache_read_tokens': int(_num(it.get('cache_read_tokens'))),
                'cache_write_tokens': int(_num(it.get('cache_write_tokens'))), 'cost_usd': round(_num(it.get('cost_usd')), 4),
                'models': models, 'last_at': it.get('last_at', ''), 'last_model': it.get('last_model', '')}

    out = {'ok': True, 'today': today.isoformat(), 'total': pack(total),
           'days': [{'day': it['k'][4:], **pack(it)} for it in days[-45:]],
           'month_cost_usd': round(sum(_num(it.get('cost_usd')) for it in days if it['k'][4:].startswith(month)), 4),
           'today_cost_usd': round(_num(next((it.get('cost_usd') for it in days if it['k'][4:] == today.isoformat()), 0)), 4),
           'prices': {row[0]: {'input': row[1], 'output': row[2], 'cache_write': row[3], 'cache_read': row[4]} for row in PRICES},
           'credits': None}
    if credits:
        spent_since = _num(total.get('cost_usd')) - _num(credits.get('spent_before'))
        out['credits'] = {'balance_usd': _num(credits.get('balance_usd')), 'as_of': credits.get('as_of', ''),
                          'set_by': credits.get('set_by', ''), 'spent_since_usd': round(spent_since, 4),
                          'remaining_usd': round(_num(credits.get('balance_usd')) - spent_since, 2)}
        # Days left at the last 14 days' pace — the number the question is about.
        recent = [it for it in days if it['k'][4:] >= (today - timedelta(days=14)).isoformat()]
        per_day = sum(_num(it.get('cost_usd')) for it in recent) / 14
        out['credits']['per_day_usd'] = round(per_day, 2)
        out['credits']['days_left'] = int(out['credits']['remaining_usd'] / per_day) if per_day > 0 else None
    return out
