"""The billing team's own record of the checks it received — the Insurance
Check Tracker spreadsheet in SharePoint.

2026-09-23, the operator: the tracker says which checks Helixona has and,
from its dates, where the scan should be (check 4121991, received 6/3/2026,
is in Unposted Checks/06-03-2026). "The checks are not always in the exact
folder, so the idea is to search all of them and match." The scans of every
folder are already read and keyed by check number, so a tracker row and its
scan meet on the same row whatever folder the scan was filed in.

What the tracker adds is the thing no other source says: **Helixona received
this check.** Blue Shield says it was cashed, eCW says whether it was posted,
the 835 says it was sent — only the team's own sheet says it arrived.

The sheet, as the team keeps it (screenshot, 2026-09-23):

    Date Check Received · Check Date · Deposit Date · Insurance Company ·
    Check Number · Amount Paid · Posted to Account (Yes/No)

Columns are matched by name, on every sheet, from whichever of the first
rows holds them. Nothing is written back: the file is downloaded, read and
left as it is.
"""
import datetime
import os
import re
import time
from urllib.parse import parse_qs, unquote, urlparse, quote

from src.utils.logger import get_logger

logger = get_logger(__name__)

# The sharing link the operator gave (2026-09-23).
DEFAULT_TRACKER_LINK = ('https://helixona.sharepoint.com/:x:/s/BillingDepartment/'
                        'IQCRf7lyZ1jsQ7tLJujIe3IXAWYERQGIjiKgRZFHvkxUe34?e=AxylEt')
# Where it lives, if the link cannot be followed: the newest spreadsheet here.
DEFAULT_TRACKER_FOLDER = '/sites/BillingDepartment/Shared Documents/Insurance Checks/Insurance Check Tracker'
SHEET_EXT = ('.xlsx', '.xlsm', '.xls', '.csv')

COLUMNS = {
    'check_no': re.compile(r'check\s*(number|no\.?|num|#)', re.I),
    'amount': re.compile(r'amount|paid', re.I),
    'received': re.compile(r'received', re.I),
    'check_date': re.compile(r'^check\s*date', re.I),
    'deposit_date': re.compile(r'deposit', re.I),
    'payer': re.compile(r'insurance|payer|company', re.I),
    'posted': re.compile(r'posted', re.I),
}
REQUIRED = ('check_no',)
HEADER_SCAN_ROWS = 15


# ------------------------------------------------------------- the reading
def _cell_text(v):
    if v is None:
        return ''
    if isinstance(v, datetime.datetime):
        return v.strftime('%m/%d/%Y')
    if isinstance(v, datetime.date):
        return v.strftime('%m/%d/%Y')
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _check_number(v):
    """Digits only. A number typed into Excel comes back as 4121991.0."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r'\D', '', str(v if v is not None else ''))


def _money(v):
    if isinstance(v, (int, float)):
        return f'{v:.2f}'
    t = str(v or '').replace('$', '').replace(',', '').strip()
    return f'{float(t):.2f}' if re.fullmatch(r'-?\d+(\.\d+)?', t) else ''


def _header_map(row):
    """{field: column index} for a row of header cells, each header used once."""
    cells = [_cell_text(c) for c in row]
    cols = {}
    for key, rx in COLUMNS.items():
        for i, h in enumerate(cells):
            if h and i not in cols.values() and rx.search(h):
                # 'Check Date' is not the check number; 'Amount Paid' is not the posted flag.
                if key == 'check_no' and re.search(r'date', h, re.I):
                    continue
                if key == 'amount' and re.search(r'posted', h, re.I):
                    continue
                cols[key] = i
                break
    return cols


def rows_from_sheet(name, rows):
    """The tracker rows of one sheet: `rows` is a list of cell lists, the
    header somewhere in the first HEADER_SCAN_ROWS. [] when no header is found."""
    for h_i, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cols = _header_map(row)
        if all(k in cols for k in REQUIRED) and len(cols) >= 3:
            break
    else:
        return []
    out = []
    for r_i, row in enumerate(rows[h_i + 1:], start=h_i + 2):
        cell = lambda k: (row[cols[k]] if k in cols and cols[k] < len(row) else None)
        num = _check_number(cell('check_no'))
        if not re.fullmatch(r'\d{4,15}', num):
            continue
        out.append({
            'check_no': num,
            'amount': _money(cell('amount')),
            'received': _cell_text(cell('received')),
            'check_date': _cell_text(cell('check_date')),
            'deposit_date': _cell_text(cell('deposit_date')),
            'payer': _cell_text(cell('payer')),
            'posted': _cell_text(cell('posted')),
            'sheet': name,
            'row': r_i,
        })
    return out


def parse_tracker(path):
    """Every tracker row in the file, from every sheet."""
    if path.lower().endswith('.csv'):
        import csv
        with open(path, newline='', encoding='utf-8-sig') as fh:
            return rows_from_sheet(os.path.basename(path), [list(r) for r in csv.reader(fh)])
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            got = rows_from_sheet(ws.title, [list(r) for r in ws.iter_rows(values_only=True)])
            if got:
                logger.info(f"  📒 sheet {ws.title!r}: {len(got)} check(s)")
            out.extend(got)
    finally:
        wb.close()
    return out


def _date(text):
    m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', str(text or ''))
    if not m:
        return None
    mo, d, y = (int(x) for x in m.groups())
    y += 2000 if y < 100 else 0
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def since_filter(rows, since):
    """The rows received on or after `since` (MM/DD/YYYY). The tracker goes
    back years; the reconciliation window does not. A row with no readable
    date is kept — better one lookup too many than a check quietly dropped."""
    start = _date(since)
    if not start:
        return list(rows)
    out = []
    for t in rows:
        when = _date(t.get('received')) or _date(t.get('check_date')) or _date(t.get('deposit_date'))
        if when is None or when >= start:
            out.append(t)
    return out


# ------------------------------------------------------------ the fetching
def _file_item(page, site, guid):
    """The tracker file from its id, with a URL that returns its bytes."""
    host = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    api = f"{site}/_api/web/GetFileById('{guid}')"
    resp = page.request.get(f"{api}?$select=Name,ServerRelativeUrl,ETag,TimeLastModified",
                            headers={'Accept': 'application/json;odata=nometadata'})
    if not resp.ok:
        raise RuntimeError(f'SharePoint GetFileById {resp.status}')
    meta = resp.json()
    return {'name': meta.get('Name', 'tracker.xlsx'), 'path': meta.get('ServerRelativeUrl', ''),
            'etag': meta.get('ETag', ''), 'modified': meta.get('TimeLastModified', ''),
            'web_url': host + quote(meta.get('ServerRelativeUrl', '')),
            # /$value is the file's bytes; the plain URL of a spreadsheet opens Excel Online.
            'download_url': f"{api}/$value"}


def _guid_from(url):
    q = parse_qs(urlparse(url).query)
    raw = unquote((q.get('sourcedoc') or [''])[0]).strip('{}')
    return raw if re.fullmatch(r'[0-9A-Fa-f-]{32,36}', raw) else ''


def find_tracker(page, link=None, folder=None):
    """(item, site) for the tracker: follow the sharing link to the file's
    id, else take the newest spreadsheet in the tracker's folder."""
    from src.checks import sharepoint_browser as spb
    link = link or DEFAULT_TRACKER_LINK
    site, _ = spb.open_folder(page, link=link)
    # The sharing link redirects to Excel Online (Doc.aspx?sourcedoc={id});
    # give the redirect a moment before concluding it named no file.
    guid, deadline = '', time.time() + 20
    while not guid and time.time() < deadline:
        guid = _guid_from(spb._url(page))
        if not guid:
            time.sleep(1)
    if guid:
        item = _file_item(page, site, guid)
        logger.info(f"  📒 tracker: {item['name']} (modified {item['modified'][:10]})")
        return item, site
    folder = folder or DEFAULT_TRACKER_FOLDER
    logger.info(f"  (the link did not name the file — looking in {folder})")
    files = spb.list_folder(page, site, folder, skip=(), extensions=SHEET_EXT)
    if not files:
        raise RuntimeError(f'no spreadsheet in {folder} — pass tracker_link or tracker_folder')
    item = sorted(files, key=lambda f: f.get('modified') or '')[-1]
    logger.info(f"  📒 tracker: {item['name']} — the newest of {len(files)} spreadsheet(s)")
    return item, site


def read_tracker(page, dest_dir, link=None, folder=None):
    """Every row of the team's tracker. Raises when it cannot be read — the
    caller keeps what an earlier run found."""
    from src.checks import sharepoint_browser as spb
    item, _ = find_tracker(page, link, folder)
    path = spb.download(page, item, dest_dir)
    rows = parse_tracker(path)
    logger.info(f"📒 tracker: {len(rows)} check(s) the team logged as received")
    return rows
