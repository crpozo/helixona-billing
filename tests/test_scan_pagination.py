"""Nothing may read the claims table one page at a time.

DynamoDB's scan returns at most 1MB and stops there without saying so. The
submission step hit exactly that: it saw 706 of 1477 claims and reported "0
claims ready" while 87 sat on pages it never asked for. The bot looked idle and
correct while doing none of its work.
"""
import os
import re
import unittest

from src.aws.clients import scan_all

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(REPO, name), encoding='utf-8') as fh:
        return fh.read()


class PagedTable:
    """Three pages, so a single-page read is obviously wrong."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def scan(self, **kw):
        self.calls.append(kw)
        idx = kw.get('ExclusiveStartKey', {}).get('n', 0)
        items = self.pages[idx]
        out = {'Items': items}
        if idx + 1 < len(self.pages):
            out['LastEvaluatedKey'] = {'n': idx + 1}
        return out


class ScanAllFollowsPagination(unittest.TestCase):
    def setUp(self):
        self.table = PagedTable([
            [{'claim_id': str(i)} for i in range(0, 600)],
            [{'claim_id': str(i)} for i in range(600, 1200)],
            [{'claim_id': str(i)} for i in range(1200, 1477)],
        ])

    def test_it_returns_every_page(self):
        self.assertEqual(len(scan_all(self.table)), 1477)

    def test_a_single_page_would_have_missed_most_of_it(self):
        self.assertEqual(len(self.table.scan()['Items']), 600)

    def test_the_last_page_is_not_the_one_dropped(self):
        ids = {c['claim_id'] for c in scan_all(self.table)}
        self.assertIn('1476', ids)

    def test_it_carries_the_cursor_forward(self):
        scan_all(self.table)
        self.assertEqual(len(self.table.calls), 3)
        self.assertNotIn('ExclusiveStartKey', self.table.calls[0])
        self.assertIn('ExclusiveStartKey', self.table.calls[1])

    def test_extra_kwargs_survive_every_page(self):
        scan_all(self.table, ProjectionExpression='claim_id')
        for call in self.table.calls:
            self.assertEqual(call.get('ProjectionExpression'), 'claim_id')

    def test_a_single_page_table_still_works(self):
        self.assertEqual(len(scan_all(PagedTable([[{'claim_id': 'x'}]]))), 1)


class NoBareScansRemain(unittest.TestCase):
    """One implementation, used everywhere."""

    def test_the_agent_never_scans_a_single_page(self):
        src = _read('src/main.py')
        self.assertNotIn('claims_table.scan()', src)
        self.assertIn('scan_all(claims_table)', src)

    def test_the_dashboard_uses_the_same_helper(self):
        src = _read('dashboard.py')
        self.assertIn('from src.aws.clients import scan_all', src)
        # Not its own copy of the loop.
        self.assertNotIn('def _scan_all(', src)

    def test_no_module_reimplements_the_loop(self):
        bodies = [_read('src/main.py'), _read('dashboard.py')]
        for src in bodies:
            self.assertNotIn("while True:\n        resp = table.scan(", src)


if __name__ == '__main__':
    unittest.main()
