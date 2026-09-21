"""Delete named claims from helixona-claims, after writing them to a file.

2026-09-21: 21 claims the dashboard still counted as ours to send had moved
on in eCW. The bot's own answer is to archive them (ecw_visible False) — they
leave the dashboard and keep their history — and that is what
"Refresh what eCW still lists" does. This script is the other answer, the one
the operator asked for: the rows go.

It is deliberately awkward. It prints what it will delete, it refuses to do
anything without --yes, and it writes every row to a JSON file first, so a
deletion made by mistake can be put back with --restore.

The documents themselves are not touched: an HCFA or IV Note already in S3
stays there. Only the claim rows go, which is what makes a restore possible.

    python3 scripts/delete_claims.py 2193 2535 2753            # a dry run
    python3 scripts/delete_claims.py 2193 2535 2753 --yes      # do it
    python3 scripts/delete_claims.py --restore deleted_claims_2026-09-21T18-00-00.json
"""
import argparse
import datetime
import json
import os
import sys

TABLE = os.environ.get('CLAIMS_TABLE', 'helixona-claims')
REGION = os.environ.get('AWS_REGION', 'us-west-2')
DOCS = ('hcfa_s3_path', 'prog_notes_s3_path', 'encounter_file_s3_path')


def _table():
    import boto3
    return boto3.resource('dynamodb', region_name=REGION).Table(TABLE)


def _plain(v):
    """DynamoDB hands back Decimals, which json cannot write."""
    from decimal import Decimal
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, set, tuple)):
        return [_plain(x) for x in v]
    return v


def describe(item):
    docs = [k.split('_')[0] for k in DOCS if item.get(k)]
    return (f"{item['claim_id']:>8}  {str(item.get('patient_name') or '—')[:28]:<28} "
            f"{str(item.get('service_date') or item.get('dos') or '—'):<12} "
            f"{str(item.get('charges') or '—'):>10}  "
            f"{'sent' if item.get('symplisend_submitted') else 'not sent':<9} "
            f"{', '.join(docs) or 'no documents'}")


def delete(claim_ids, confirmed):
    table = _table()
    found, missing = [], []
    for cid in claim_ids:
        item = table.get_item(Key={'claim_id': str(cid)}).get('Item')
        (found if item else missing).append(item or cid)

    if missing:
        print(f"\nNot in the table (nothing to delete): {', '.join(str(m) for m in missing)}")
    if not found:
        print("\nNothing to do.")
        return 0

    print(f"\n{len(found)} claim(s) in {TABLE}:\n")
    print(f"{'CLAIM':>8}  {'PATIENT':<28} {'DOS':<12} {'CHARGES':>10}  {'STATUS':<9} DOCUMENTS")
    for item in sorted(found, key=lambda i: str(i['claim_id'])):
        print(describe(item))

    sent = [i for i in found if i.get('symplisend_submitted')]
    if sent:
        print(f"\n⚠️  {len(sent)} of these were already submitted to SympliSend. "
              f"Deleting the row loses that record: {', '.join(str(i['claim_id']) for i in sent)}")

    if not confirmed:
        print("\nDry run. Nothing was deleted. Add --yes to go ahead.")
        return 0

    stamp = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S')
    backup = f'deleted_claims_{stamp}.json'
    with open(backup, 'w', encoding='utf-8') as fh:
        json.dump([_plain(i) for i in found], fh, indent=2, sort_keys=True)
    print(f"\n💾 {len(found)} row(s) written to {backup} — keep it; --restore reads it back.")

    gone = 0
    for item in found:
        table.delete_item(Key={'claim_id': item['claim_id']})
        gone += 1
        print(f"  🗑  deleted claim {item['claim_id']}")
    print(f"\nDeleted {gone} claim(s). The documents in S3 were not touched.")
    return gone


def restore(path):
    table = _table()
    with open(path, encoding='utf-8') as fh:
        rows = json.load(fh)
    from decimal import Decimal

    def back(v):
        return Decimal(str(v)) if isinstance(v, float) else v
    for row in rows:
        table.put_item(Item={k: back(v) for k, v in row.items()})
        print(f"  ↩  restored claim {row['claim_id']}")
    print(f"\nRestored {len(rows)} claim(s) from {path}")
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('claim_ids', nargs='*', help='the claim numbers to delete')
    ap.add_argument('--yes', action='store_true', help='actually delete (without it, a dry run)')
    ap.add_argument('--restore', metavar='FILE', help='put back the rows a previous run wrote')
    args = ap.parse_args(argv)

    if args.restore:
        return 0 if restore(args.restore) else 1
    if not args.claim_ids:
        ap.error('name the claims to delete, or pass --restore FILE')
    ids = [c.strip().strip(',') for c in args.claim_ids if c.strip().strip(',')]
    delete(ids, args.yes)
    return 0


if __name__ == '__main__':
    sys.exit(main())
