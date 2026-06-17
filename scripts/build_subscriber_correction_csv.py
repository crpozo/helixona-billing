"""
Build the subscriber_id correction CSV.

Ground truth = the HCFA-1500 PDF that ECW generated for each claim. ECW renders
box 1a ("Insured's I.D. Number") from its OWN database, independent of our buggy
scrape — so box 1a is the correct PRIMARY subscriber id per claim. (Verified: the
id always sits at the top-right box-1a region x~0.61, y~0.13, exactly one per form.)

For every claim we compare the stored subscriber_id (DynamoDB) against the box-1a
value from its HCFA PDF and flag MISMATCH / MATCH / NO_PDF / NO_ID_FOUND.

Output: /tmp/subscriber_corrections.csv  AND  ./subscriber_corrections.csv (repo root).

Run:
    cd "<repo>" && set -a && . ./.env && set +a && ./venv/bin/python scripts/build_subscriber_correction_csv.py
"""
import csv
import os
import re
import boto3
import pdfplumber

REGION = 'us-west-2'
# box 1a region on the HCFA-1500 page (fractions of page width/height)
BOX1A_X = (0.55, 0.92)
BOX1A_Y = (0.09, 0.20)
# permissive insured-id token: >=8 alnum chars containing >=4 digits (covers
# R60056135, XED913505676, OCO903699558, FNN100011240352, WUE9404090GJ, ...)
ID_TOKEN = re.compile(r'^[A-Z0-9]{8,}$')

s3 = boto3.client('s3', region_name=REGION)
table = boto3.resource('dynamodb', region_name=REGION).Table('helixona-claims')


def scan_all():
    items, kw = [], {}
    while True:
        r = table.scan(**kw)
        items.extend(r.get('Items', []))
        if not r.get('LastEvaluatedKey'):
            return items
        kw['ExclusiveStartKey'] = r['LastEvaluatedKey']


def download(s3_path, cid):
    if not s3_path.startswith('s3://'):
        return None
    bucket, key = s3_path[5:].split('/', 1)
    local = f'/tmp/hcfa_cache_{cid}.pdf'
    if not os.path.exists(local):
        try:
            s3.download_file(bucket, key, local)
        except Exception as e:
            print(f"  [{cid}] download failed: {e}")
            return None
    return local


def box1a_id(local):
    """Return (value, n_candidates) for the box-1a insured id on page 1."""
    try:
        with pdfplumber.open(local) as doc:
            pg = doc.pages[0]
            W, H = pg.width, pg.height
            words = pg.extract_words()
    except Exception as e:
        return None, 0, f'pdf_error:{e}'
    cands = []
    for w in words:
        tok = w['text'].strip().upper()
        if not ID_TOKEN.match(tok):
            continue
        if sum(c.isdigit() for c in tok) < 4:
            continue
        fx, fy = w['x0'] / W, w['top'] / H
        in_box = (BOX1A_X[0] <= fx <= BOX1A_X[1]) and (BOX1A_Y[0] <= fy <= BOX1A_Y[1])
        cands.append((fy, fx, tok, in_box))
    in_box = [c for c in cands if c[3]]
    pool = in_box if in_box else [c for c in cands if c[0] <= 0.22]
    if not pool:
        return None, len(cands), 'no_id_in_region'
    pool.sort(key=lambda c: (c[0], c[1]))  # topmost, then leftmost
    return pool[0][2], len(pool), ('box1a' if in_box else 'upper_fallback')


def main():
    items = scan_all()
    rows = []
    for it in sorted(items, key=lambda x: str(x.get('claim_id', ''))):
        cid = str(it.get('claim_id', ''))
        stored = (it.get('subscriber_id') or '').strip()
        path = it.get('hcfa_s3_path', '')
        local = download(path, cid)
        correct, ncand, src = (None, 0, 'no_pdf') if not local else box1a_id(local)
        if correct is None:
            status = 'NO_PDF' if not local else 'NO_ID_FOUND'
        elif correct.upper() == stored.upper():
            status = 'MATCH'
        else:
            status = 'MISMATCH'
        rows.append({
            'claim_id': cid,
            'patient_name': it.get('patient_name', ''),
            'payer': it.get('payer', ''),
            'dos': it.get('dos', ''),
            'stored_subscriber_id': stored,
            'correct_subscriber_id': correct or '',
            'status': status,
            'id_source': src,
            'symplisend_submitted': bool(it.get('symplisend_submitted')),
            'symplisend_fln': it.get('symplisend_fln', ''),
            'hcfa_generated_at': it.get('hcfa_generated_at', ''),
            'hcfa_s3_path': path,
        })

    cols = ['claim_id', 'patient_name', 'payer', 'dos', 'stored_subscriber_id',
            'correct_subscriber_id', 'status', 'id_source', 'symplisend_submitted',
            'symplisend_fln', 'hcfa_generated_at', 'hcfa_s3_path']
    for out in ('/tmp/subscriber_corrections.csv', 'subscriber_corrections.csv'):
        with open(out, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            # incorrect first, sorted by claim
            w.writerows(sorted(rows, key=lambda r: (r['status'] != 'MISMATCH', r['claim_id'])))

    n = len(rows)
    mism = [r for r in rows if r['status'] == 'MISMATCH']
    match = [r for r in rows if r['status'] == 'MATCH']
    nopdf = [r for r in rows if r['status'] in ('NO_PDF', 'NO_ID_FOUND')]
    mism_sub = [r for r in mism if r['symplisend_submitted']]

    print(f"\n{'='*70}")
    print("  SUBSCRIBER CORRECTION REPORT  (ground truth = HCFA box 1a)")
    print(f"{'='*70}")
    print(f"  Total claims ........................ {n}")
    print(f"  ✅ MATCH (stored == PDF) ............ {len(match)}")
    print(f"  ❌ MISMATCH (needs correction) ...... {len(mism)}")
    print(f"       of those, already submitted .... {len(mism_sub)}")
    print(f"  ⚠️  No PDF / no id found ............ {len(nopdf)}")
    print(f"\n  CSV: /tmp/subscriber_corrections.csv  and  ./subscriber_corrections.csv")
    print(f"{'='*70}\n")
    print("  Incorrect claims (stored  ->  correct):")
    for r in sorted(mism, key=lambda r: int(r['claim_id']) if r['claim_id'].isdigit() else 0):
        sub = 'SUBMITTED' if r['symplisend_submitted'] else 'not-submitted'
        print(f"   {r['claim_id']:>6}  {r['patient_name'][:26]:<26}  "
              f"{r['stored_subscriber_id']:<16} -> {r['correct_subscriber_id']:<16} [{sub}]")


if __name__ == '__main__':
    main()
