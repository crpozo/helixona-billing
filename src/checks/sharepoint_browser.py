"""The Insurance Checks folder through the bot's own browser — a Helixona
account, signed in once on the live screen.

The folder is shared inside Helixona only, and the app-registration route
(src/checks/sharepoint.py) needs an Entra admin. The bot already drives a
Chrome with a persistent profile, so a person signs in to SharePoint once
in the noVNC window (Microsoft's own MFA, "Stay signed in?" → Yes) and from
then on every run reads the folder with that session: SharePoint's REST API
(`/_api/web/GetFolderByServerRelativeUrl`) called with the browser's cookies
through page.request, files downloaded the same way. Nothing is written.

Where the folder is comes from its sharing link — opened in the browser,
SharePoint lands on the library view whose `id=` parameter is the folder's
server-relative path. The link lives in the secret `sharepoint_credentials`
(`share_link`), in the task body (`share_link`), or, failing both, here.
`username` / `password` in the secret are typed on Microsoft's sign-in page
when it appears; the MFA prompt is always the person's to answer.
"""
import os
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

from src.checks.sharepoint import IMAGE_EXT
from src.utils.logger import get_logger

logger = get_logger(__name__)

# The BillingDepartment site's "Insurance Checks" folder, as shared on 2026-09-17.
DEFAULT_SHARE_LINK = ('https://helixona.sharepoint.com/:f:/s/BillingDepartment/'
                      'IgAYNAnmSwIdQpWut5OQKJFgAdhcdsUcGXqMk2MM5hm8TL4?e=bWPWNm')
DEFAULT_FOLDER = '/sites/BillingDepartment/Shared Documents/Insurance Checks'
LOGIN_HOSTS = ('login.microsoftonline.com', 'login.live.com', 'login.microsoft.com', 'adfs')
# The folder, as the department keeps it (2026-09-17):
#   Insurance Checks / Posted Checks / <year> / <month> / <scans>
#                    / Unposted Checks / <date> / <scans>
#                    / Insurance Check Tracker   (their spreadsheet, not scans)
# Posted Checks/2025 is before the period reconciled (the operator said to
# leave it out), so it is not read.
SKIP_FOLDERS = ('Forms', 'Insurance Check Tracker', 'Posted Checks/2025')
SITE_RX = re.compile(r'^(https://[^/]+/sites/[^/?#]+)', re.I)
JSON_HEADERS = {'Accept': 'application/json;odata=nometadata'}


def _url(page):
    try:
        return page.url or ''
    except Exception:
        return ''


def _at_login(url):
    host = urlparse(url).netloc.lower()
    return any(h in host for h in LOGIN_HOSTS)


def _try(page, selector, action, value=None, timeout=3000):
    try:
        loc = page.locator(selector).first
        if action == 'fill':
            loc.fill(value, timeout=timeout)
        else:
            loc.click(timeout=timeout)
        return True
    except Exception:
        return False


def _type_credentials(page, creds):
    """Microsoft's sign-in: e-mail → Next, password → Sign in. Whatever it
    asks next (the MFA prompt) is the person's."""
    user, pwd = (creds or {}).get('username', ''), (creds or {}).get('password', '')
    if user and _try(page, 'input[type="email"], #i0116, input[name="loginfmt"]', 'fill', user):
        _try(page, '#idSIButton9, input[type="submit"]', 'click')
        logger.info("  ✅ e-mail typed on Microsoft's sign-in")
        time.sleep(3)
    if pwd and _try(page, 'input[type="password"], #i0118, input[name="passwd"]', 'fill', pwd):
        _try(page, '#idSIButton9, input[type="submit"]', 'click')
        logger.info("  ✅ password typed")
        time.sleep(3)


def _stay_signed_in(page):
    """The 'Stay signed in?' page: Yes, so the profile keeps the session."""
    try:
        if page.locator('#KmsiCheckboxField').count():
            _try(page, '#KmsiCheckboxField', 'click')
        if page.get_by_text('Stay signed in?').count():
            _try(page, '#idSIButton9, input[type="submit"]', 'click')
            return True
    except Exception:
        pass
    return False


def open_folder(page, link=None, creds=None, wait_for_person=240):
    """Open the sharing link; sign in if Microsoft asks (credentials typed,
    MFA waited for on the live screen); return (site_url, folder_path)."""
    link = link or (creds or {}).get('share_link') or DEFAULT_SHARE_LINK
    logger.info(f"📂 SharePoint: opening {link[:80]}…")
    page.goto(link, wait_until='domcontentloaded', timeout=60000)
    time.sleep(4)
    typed = False
    deadline = time.time() + wait_for_person
    warned = False
    while True:
        url = _url(page)
        if _stay_signed_in(page):
            time.sleep(4)
            continue
        if 'sharepoint.com' in urlparse(url).netloc.lower() and not _at_login(url):
            break
        if _at_login(url) and not typed:
            _type_credentials(page, creds)
            typed = True
            continue
        if not warned:
            logger.warning("  ⚠️ SharePoint wants a sign-in — finish it on the live screen (noVNC), "
                           f"tick 'Stay signed in'; waiting up to {wait_for_person}s")
            warned = True
        if time.time() > deadline:
            raise RuntimeError('SharePoint sign-in not completed on the live screen')
        time.sleep(3)
    time.sleep(3)
    url = _url(page)
    site = (SITE_RX.match(url) or SITE_RX.match(link)).group(1)
    folder = unquote((parse_qs(urlparse(url).query).get('id') or [''])[0]) \
        or (creds or {}).get('folder_url') or DEFAULT_FOLDER
    logger.info(f"  ✅ signed in · site {site} · folder {folder}")
    return site, folder


def _api(page, site, folder, what, select, tries=3):
    """GET /_api/web/GetFolderByServerRelativePath(decodedurl=@f)/<what>
    with the browser's cookies. `what` is 'Files' or 'Folders'. A 5xx is
    retried: SharePoint answered 503 on a folder named "01'2026" once."""
    url = (f"{site}/_api/web/GetFolderByServerRelativePath(decodedurl=@f)/{what}"
           f"?@f='{quote(folder.replace(chr(39), chr(39) * 2), safe='')}'&$select={select}&$top=5000")
    last = None
    for attempt in range(tries):
        resp = page.request.get(url, headers=JSON_HEADERS)
        if resp.ok:
            return resp.json()
        last = resp.status
        if resp.status < 500:
            break
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'SharePoint API {what} {last} for {folder}')


def _skipped(rel, skip):
    low = rel.lower()
    return any(low == s.lower() or low.startswith(s.lower() + '/') for s in skip)


def list_folder(page, site, folder, skip=SKIP_FOLDERS):
    """Every image under the folder (subfolders included, `skip` left out),
    in the shape of sharepoint.list_check_files: {'id','name','path','size',
    'etag','modified','web_url','download_url'}. `path` is relative to the
    folder — 'Posted Checks/2026/07-2026/scan.pdf' — so it says where the
    department filed the cheque."""
    host = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    out = []
    skipped = []
    left_out = []

    def walk(rel_folder, rel):
        try:
            files = _api(page, site, rel_folder, 'Files', 'Name,ServerRelativeUrl,TimeLastModified,Length,UniqueId,ETag')
        except Exception as e:
            if not rel:
                raise
            # One subfolder SharePoint will not list is not the whole folder:
            # note it, keep going, so the run still reads everything else.
            skipped.append(rel)
            logger.warning(f"  ⚠️ subfolder {rel!r} could not be listed ({str(e)[:80]}) — skipped this run")
            return
        for it in files.get('value', []):
            name = it.get('Name', '')
            if not name.lower().endswith(IMAGE_EXT):
                continue
            out.append({
                'id': it.get('UniqueId', ''), 'name': name, 'path': f"{rel}/{name}" if rel else name,
                'size': int(it.get('Length') or 0), 'etag': it.get('ETag', ''),
                'modified': it.get('TimeLastModified', ''),
                'web_url': host + quote(it.get('ServerRelativeUrl', '')),
                'download_url': host + quote(it.get('ServerRelativeUrl', '')),
            })
        try:
            subs = _api(page, site, rel_folder, 'Folders', 'Name,ServerRelativeUrl')
        except Exception as e:
            skipped.append(rel + '/*')
            logger.warning(f"  ⚠️ subfolders of {rel or '/'!r} could not be listed ({str(e)[:80]}) — skipped this run")
            return
        for sf in subs.get('value', []):
            name = sf.get('Name', '')
            if not name:
                continue
            sub_rel = f"{rel}/{name}" if rel else name
            if _skipped(sub_rel, skip):
                left_out.append(sub_rel)
                continue
            walk(sf.get('ServerRelativeUrl') or f"{rel_folder}/{name}", sub_rel)

    walk(folder, '')
    logger.info(f"📁 SharePoint {folder.rsplit('/', 1)[-1]}: {len(out)} file(s)"
                + (f" · left out on purpose: {left_out}" if left_out else '')
                + (f" · {len(skipped)} subfolder(s) could not be listed: {skipped[:5]}" if skipped else ''))
    return out


def download(page, item, dest_dir):
    """The file's bytes through the browser's session."""
    path = os.path.join(dest_dir, re.sub(r'[^A-Za-z0-9._-]', '_', item['name']))
    resp = page.request.get(item['download_url'])
    if not resp.ok:
        raise RuntimeError(f"download {resp.status} for {item['path']}")
    with open(path, 'wb') as fh:
        fh.write(resp.body())
    return path
