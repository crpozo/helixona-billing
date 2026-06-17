"""
Audit subscriber_id quality in helixona-claims.

Read-only. Classifies every captured subscriber_id as conforming (^R\\d{8}$, the
Blue Shield format the user confirmed, e.g. R60056135) vs non-conforming, flags
values shared across multiple patients (a fingerprint of the stale-popup / wrong-
node extraction bug), and — most importantly — lists the claims that were ALREADY
submitted to SympliSend with a non-conforming subscriber_id (symplisend_submitted=True),
since those went out to the payer with a wrong member number.

Writes a CSV to /tmp/subscriber_audit.csv and prints a summary.

Run:
    cd "<repo>" && set -a && . ./.env && set +a && ./venv/bin/python scripts/audit_subscriber_ids.py
"""
import csv
import re
import collections
import boto3

RX = re.compile(r'^R\d{8}$')  # confirmed Blue Shield subscriber format

table = boto3.resource('dynamodb', region_name='us-west-2').Table('helixona-claims')


def _scan_all():
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            return items
        kwargs['ExclusiveStartKey'] = lek


def main():
    items = _scan_all()
    rows = []
    for it in items:
        sub = (it.get('subscriber_id') or '').strip()
        if not sub:
            continue
        rows.append({
            'claim_id': str(it.get('claim_id', '')),
            'patient_name': it.get('patient_name', ''),
            'subscriber_id': sub,
            'conforming': bool(RX.match(sub)),
            'submission_type': it.get('submission_type', ''),
            'symplisend_submitted': bool(it.get('symplisend_submitted')),
            'symplisend_fln': it.get('symplisend_fln', ''),
            'state': int(it.get('state', 0) or 0),
        })

    total = len(items)
    with_sub = len(rows)
    conforming = [r for r in rows if r['conforming']]
    bad = [r for r in rows if not r['conforming']]

    # Values shared across >1 distinct claim — impossible for a real subscriber id.
    freq = collections.Counter(r['subscriber_id'] for r in rows)
    shared = {v: n for v, n in freq.items() if n > 1}

    # The urgent set: non-conforming AND already submitted to SympliSend.
    submitted_bad = [r for r in bad if r['symplisend_submitted']]
    submitted_conf = [r for r in conforming if r['symplisend_submitted']]

    # Write full CSV
    out = '/tmp/subscriber_audit.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=[
            'claim_id', 'patient_name', 'subscriber_id', 'conforming',
            'submission_type', 'symplisend_submitted', 'symplisend_fln', 'state',
        ])
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x['conforming'], x['subscriber_id'], x['claim_id'])):
            w.writerow(r)

    print(f"\n{'='*72}")
    print("  SUBSCRIBER_ID AUDIT — helixona-claims")
    print(f"{'='*72}")
    print(f"  Total claims in table .............. {total}")
    print(f"  Claims with a subscriber_id ........ {with_sub}")
    print(f"  Conforming  (^R\\d{{8}}$) ............ {len(conforming)}")
    print(f"  NON-conforming ..................... {len(bad)}")
    print(f"  Distinct subscriber values ......... {len(freq)}")

    print(f"\n  -- Values shared across multiple patients (extraction-bug fingerprint) --")
    if shared:
        for v, n in sorted(shared.items(), key=lambda x: -x[1]):
            tag = 'OK ' if RX.match(v) else 'BAD'
            print(f"     {n:>3} claims  [{tag}]  {v}")
    else:
        print("     (none)")

    print(f"\n  {'!'*68}")
    print(f"  URGENT: submitted to SympliSend WITH a non-conforming subscriber_id")
    print(f"  {'!'*68}")
    print(f"  Count: {len(submitted_bad)}")
    for r in sorted(submitted_bad, key=lambda x: x['claim_id']):
        print(f"     claim {r['claim_id']:>6}  sub={r['subscriber_id']:<16} "
              f"FLN={r['symplisend_fln'] or '-':<14} {r['patient_name']}")

    print(f"\n  Submitted WITH a conforming subscriber_id (likely OK): {len(submitted_conf)}")
    for r in sorted(submitted_conf, key=lambda x: x['claim_id']):
        print(f"     claim {r['claim_id']:>6}  sub={r['subscriber_id']:<16} "
              f"FLN={r['symplisend_fln'] or '-':<14} {r['patient_name']}")

    print(f"\n  Full CSV written to: {out}")
    print(f"{'='*72}\n")


if __name__ == '__main__':
    main()
