"""The one-off that removes named claims from helixona-claims.

2026-09-21: 21 claims the dashboard counted as ours to send had moved on in
eCW, and the operator asked for the rows to go rather than be archived. A
deletion cannot be undone, so this one prints first, refuses without --yes,
and writes every row to a file it can read back.
"""
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'scripts'))
import delete_claims as dc


class _Table:
    def __init__(self, items):
        self.items = {i['claim_id']: dict(i) for i in items}
        self.deleted = []

    def get_item(self, Key):
        it = self.items.get(Key['claim_id'])
        return {'Item': dict(it)} if it else {}

    def delete_item(self, Key):
        self.deleted.append(Key['claim_id'])
        self.items.pop(Key['claim_id'], None)

    def put_item(self, Item):
        self.items[Item['claim_id']] = dict(Item)


class DeletingNamedClaims(unittest.TestCase):
    ROWS = [
        {'claim_id': '2193', 'patient_name': 'Spillar, Cynthia', 'service_date': '01/02/2026',
         'charges': '1.14', 'hcfa_s3_path': 's3://b/h.pdf', 'state': Decimal('2')},
        {'claim_id': '2535', 'patient_name': 'De Boer, Gloria', 'service_date': '12/03/2025',
         'symplisend_submitted': True},
    ]

    def setUp(self):
        self.table = _Table(self.ROWS)
        dc._table = lambda: self.table
        self._cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._cwd)

    def _backup(self):
        return [f for f in os.listdir(self.tmp) if f.startswith('deleted_claims_')]

    def test_without_yes_it_deletes_nothing(self):
        dc.main(['2193', '2535'])
        self.assertEqual(self.table.deleted, [])
        self.assertEqual(self._backup(), [], 'a dry run writes no file either')

    def test_a_claim_that_is_not_there_is_not_an_error(self):
        dc.main(['9999', '--yes'])
        self.assertEqual(self.table.deleted, [])

    def test_it_writes_every_row_before_deleting_it(self):
        dc.main(['2193', '2535', '--yes'])
        self.assertEqual(sorted(self.table.deleted), ['2193', '2535'])
        self.assertEqual(self.table.items, {})
        with open(os.path.join(self.tmp, self._backup()[0]), encoding='utf-8') as fh:
            saved = json.load(fh)
        self.assertEqual(sorted(r['claim_id'] for r in saved), ['2193', '2535'])
        # The Decimal DynamoDB hands back must not stop the file being written.
        self.assertEqual([r for r in saved if r['claim_id'] == '2193'][0]['state'], 2)

    def test_the_file_puts_them_back(self):
        dc.main(['2193', '2535', '--yes'])
        dc.main(['--restore', os.path.join(self.tmp, self._backup()[0])])
        self.assertEqual(sorted(self.table.items), ['2193', '2535'])
        self.assertEqual(self.table.items['2193']['hcfa_s3_path'], 's3://b/h.pdf')

    def test_the_documents_in_s3_are_never_touched(self):
        src = open(os.path.join(REPO, 'scripts/delete_claims.py'), encoding='utf-8').read()
        for forbidden in ('delete_object', 'delete_bucket', "client('s3'", "resource('s3'"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_it_names_a_claim_already_sent(self):
        # Deleting one of those loses the record that it went out.
        src = open(os.path.join(REPO, 'scripts/delete_claims.py'), encoding='utf-8').read()
        self.assertIn('already submitted to SympliSend', src)


if __name__ == '__main__':
    unittest.main()
