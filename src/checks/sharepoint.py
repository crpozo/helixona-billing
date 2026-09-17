"""The Insurance Checks folder on SharePoint — a copy of every physical cheque.

The billing department scans each cheque Blue Shield mails and files the
image under Shared Documents / Insurance Checks on the BillingDepartment
site. Those images are the first source of the reconciliation: a cheque we
hold a copy of, with its number and amount read off the image.

Access is Microsoft Graph, app-only. The secret `sharepoint_credentials`
in Secrets Manager (prefix prod/helixona/) carries:

    tenant_id, client_id, client_secret   — an Entra app registration with
                                            Sites.Read.All (application)
    site_hostname  e.g. helixona.sharepoint.com
    site_path      e.g. /sites/BillingDepartment
    folder         e.g. Insurance Checks     (inside the default library)

An app registration takes an Entra admin. Without one the run goes through
the bot's browser instead — a Helixona account signed in once on the live
screen, src/checks/sharepoint_browser.py — and, with source:'s3', through
an S3 inbox (`checks/inbox/` in the claims bucket) where images are dropped
by hand — see `list_s3_inbox`.

Nothing here writes to SharePoint.
"""
import os
import re

import requests

from src.utils.logger import get_logger

logger = get_logger(__name__)

GRAPH = 'https://graph.microsoft.com/v1.0'
IMAGE_EXT = ('.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.gif', '.webp', '.heic')
S3_INBOX_PREFIX = 'checks/inbox/'


def graph_token(creds):
    tenant = creds['tenant_id']
    resp = requests.post(
        f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token',
        data={'client_id': creds['client_id'], 'client_secret': creds['client_secret'],
              'scope': 'https://graph.microsoft.com/.default', 'grant_type': 'client_credentials'},
        timeout=30)
    resp.raise_for_status()
    return resp.json()['access_token']


def _get(token, url, params=None):
    resp = requests.get(url, headers={'Authorization': f'Bearer {token}'}, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _drive_and_folder(token, creds):
    host = creds.get('site_hostname', 'helixona.sharepoint.com')
    path = creds.get('site_path', '/sites/BillingDepartment')
    site = _get(token, f'{GRAPH}/sites/{host}:{path}')
    drive = _get(token, f"{GRAPH}/sites/{site['id']}/drive")
    return drive['id'], creds.get('folder', 'Insurance Checks')


def list_check_files(token, creds):
    """Every file under the folder (subfolders included), as
    {'id', 'name', 'path', 'size', 'etag', 'modified', 'web_url', 'download_url'}."""
    drive_id, folder = _drive_and_folder(token, creds)
    out = []

    def walk(item_path, rel):
        url = f"{GRAPH}/drives/{drive_id}/root:/{item_path}:/children"
        params = {'$top': 200}
        while url:
            data = _get(token, url, params)
            params = None
            for it in data.get('value', []):
                name = it.get('name', '')
                if 'folder' in it:
                    walk(f"{item_path}/{name}", f"{rel}/{name}" if rel else name)
                    continue
                if not name.lower().endswith(IMAGE_EXT):
                    continue
                out.append({
                    'id': it.get('id', ''),
                    'name': name,
                    'path': f"{rel}/{name}" if rel else name,
                    'size': it.get('size', 0),
                    'etag': it.get('eTag') or it.get('cTag') or '',
                    'modified': it.get('lastModifiedDateTime', ''),
                    'web_url': it.get('webUrl', ''),
                    'download_url': it.get('@microsoft.graph.downloadUrl', ''),
                })
            url = data.get('@odata.nextLink')

    walk(folder, '')
    logger.info(f"📁 SharePoint {folder}: {len(out)} file(s)")
    return out


def download(item, dest_dir):
    path = os.path.join(dest_dir, re.sub(r'[^A-Za-z0-9._-]', '_', item['name']))
    with requests.get(item['download_url'], stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, 'wb') as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
    return path


# ------------------------------------------------------------- S3 inbox
def list_s3_inbox(aws_client, bucket, prefix=S3_INBOX_PREFIX):
    """The same shape as list_check_files, for images dropped in S3 by hand."""
    out, kwargs = [], {'Bucket': bucket, 'Prefix': prefix}
    while True:
        resp = aws_client.s3.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            name = key.rsplit('/', 1)[-1]
            if not name or not name.lower().endswith(IMAGE_EXT):
                continue
            out.append({
                'id': key, 'name': name, 'path': key[len(prefix):], 'size': obj.get('Size', 0),
                'etag': (obj.get('ETag') or '').strip('"'),
                'modified': obj['LastModified'].strftime('%Y-%m-%dT%H:%M:%SZ') if obj.get('LastModified') else '',
                'web_url': f's3://{bucket}/{key}', 'download_url': '',
            })
        if not resp.get('IsTruncated'):
            break
        kwargs['ContinuationToken'] = resp['NextContinuationToken']
    logger.info(f"📁 S3 inbox {prefix}: {len(out)} file(s)")
    return out


def download_s3(aws_client, bucket, item, dest_dir):
    path = os.path.join(dest_dir, re.sub(r'[^A-Za-z0-9._-]', '_', item['name']))
    aws_client.s3.download_file(bucket, item['id'], path)
    return path
