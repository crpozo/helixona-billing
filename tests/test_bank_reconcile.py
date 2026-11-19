"""B10 — bank ↔ remittances ↔ eCW (RCM Automation Punch List v1.0).

The acceptance test: "Every test deposit accounted for once; one-to-many,
split checks, reversed payments and date differences handled without false
matches." All cases here are fabricated."""
import datetime
import os
import unittest

from src.checks import bank

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = datetime.date(2026, 11, 19)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def txn(date, amount, desc='DEPOSIT', ref='', n=1):
    rows = bank.rows_to_txns([['Date', 'Description', 'Amount', 'Check Number'], [date, desc, amount, ref]] * 1)
    t = rows[0]
    t['id'] = f"{t['id']}{n}"
    return t


def check(number, amount, verdict='posted', **kw):
    base = {'check_number': number, 'copy_amount': amount, 'has_copy': True, 'verdict': verdict, 'in_ecw': verdict in ('posted', 'unposted'),
            'ecw_unposted': '0.00' if verdict == 'posted' else amount}
    return {**base, **kw}


def run(txns, rows):
    got, missing, sm = bank.reconcile_bank(txns, rows, TODAY)
    return {r['id']: r for r in got}, missing, sm


class TheStatementIsRead(unittest.TestCase):
    def test_one_signed_amount_column(self):
        got = bank.rows_to_txns([['Account 123'], [], ['Posting Date', 'Description', 'Amount', 'Check or Slip #', 'Balance'],
                                 ['11/03/2026', 'REMOTE DEPOSIT', '1,099.18', '', '5000.00'],
                                 ['11/04/2026', 'ACH HCCLAIMPMT BLUE SHIELD CA TRN*1*31401901', '$38.81', '', ''],
                                 ['11/05/2026', 'RETURNED DEPOSITED ITEM', '-250.00', '', ''],
                                 ['11/05/2026', 'BKCRD MERCH SETTLEMENT', '400.00', '', ''],
                                 ['', 'Ending balance', '', '', '']])
        self.assertEqual([(t['date'], t['amount'], t['kind']) for t in got],
                         [('2026-11-03', '1099.18', 'deposit'), ('2026-11-04', '38.81', 'eft'),
                          ('2026-11-05', '-250.00', 'reversal'), ('2026-11-05', '400.00', 'card')])

    def test_credit_and_debit_columns_and_parentheses(self):
        got = bank.rows_to_txns([['Date', 'Memo', 'Debit', 'Credit'], ['2026-11-03', 'Deposit', '', '100.00'],
                                 ['2026-11-04', 'Check 1001', '45.10', ''], ['2026-11-05', 'Fee', '(3.00)', '']])
        self.assertEqual([t['amount'] for t in got], ['100.00', '-45.10', '-3.00'])

    def test_two_identical_lines_are_two_transactions(self):
        got = bank.rows_to_txns([['Date', 'Description', 'Amount'], ['11/03/2026', 'DEPOSIT', '50.00'], ['11/03/2026', 'DEPOSIT', '50.00']])
        self.assertEqual(len({t['id'] for t in got}), 2)

    def test_the_same_file_twice_gives_the_same_ids(self):
        rows = [['Date', 'Description', 'Amount'], ['11/03/2026', 'DEPOSIT', '50.00']]
        self.assertEqual(bank.rows_to_txns(rows)[0]['id'], bank.rows_to_txns(rows)[0]['id'])

    def test_csv_bytes_with_semicolons(self):
        got = bank.parse_statement(b'Date;Description;Amount\n11/03/2026;DEPOSIT;50.00\n', 'x.csv')
        self.assertEqual(got[0]['amount'], '50.00')

    def test_a_sheet_that_is_not_a_statement_gives_nothing(self):
        self.assertEqual(bank.rows_to_txns([['Name', 'Phone'], ['A', '1']]), [])


class OneDepositManyChecks(unittest.TestCase):
    def test_a_deposit_slip_total_matches_its_bank_credit_days_later(self):
        rows = [{'check_number': 'deposit:Unposted/11-02/dep.pdf', 'copy_file': 'Unposted/11-02/dep.pdf', 'deposit_slip': True,
                 'deposit_total': '150.00', 'deposit_date': '11/02/2026', 'deposit_checks': ['1001', '1002']},
                check('1001', '100.00', deposit_file='Unposted/11-02/dep.pdf'),
                check('1002', '50.00', verdict='not in eCW', deposit_file='Unposted/11-02/dep.pdf')]
        t = txn('11/04/2026', '150.00')
        by, missing, sm = run([t], rows)
        r = by[t['id']]
        self.assertEqual((r['status'], r['rule'], sorted(r['items']), r['variance']),
                         ('matched · not posted', 'deposit slip', ['1001', '1002'], '50.00'))
        self.assertIn('not posted in eCW: 1002', r['reason'])
        self.assertEqual(missing, [])

    def test_the_trackers_day_of_checks_adds_up_to_one_credit(self):
        rows = [check('2001', '120.00', has_copy=False, in_tracker=True, tracker_deposit='11/03/2026', tracker_amount='120.00'),
                check('2002', '80.00', has_copy=False, in_tracker=True, tracker_deposit='11/03/2026', tracker_amount='80.00')]
        t = txn('11/05/2026', '200.00')
        by, _, sm = run([t], rows)
        self.assertEqual((by[t['id']]['status'], by[t['id']]['rule'], sorted(by[t['id']]['items'])),
                         ('matched · posted', 'tracker deposit date', ['2001', '2002']))
        self.assertTrue(sm['accounted'])

    def test_one_day_of_checks_split_between_two_deposits(self):
        rows = [check(n, a, in_tracker=True, tracker_deposit='11/03/2026') for n, a in (('3001', '100.00'), ('3002', '60.00'), ('3003', '25.00'))]
        a, b = txn('11/03/2026', '125.00', n=1), txn('11/04/2026', '60.00', n=2)
        by, _, _ = run([a, b], rows)
        self.assertEqual(sorted(by[a['id']]['items']), ['3001', '3003'])
        self.assertEqual(by[b['id']]['items'], ['3002'])

    def test_a_unique_set_of_checks_explains_an_undocumented_deposit(self):
        rows = [check('4001', '71.72', tracker_received='11/01/2026', in_tracker=True),
                check('4002', '269.01', tracker_received='11/02/2026', in_tracker=True),
                check('4003', '500.00', tracker_received='11/02/2026', in_tracker=True)]
        t = txn('11/05/2026', '340.73')
        by, _, _ = run([t], rows)
        self.assertEqual((by[t['id']]['rule'], sorted(by[t['id']]['items'])), ('sum of checks', ['4001', '4002']))


class NoFalseMatches(unittest.TestCase):
    def test_two_sets_that_add_up_is_ambiguous_not_a_guess(self):
        rows = [check(n, a, tracker_received='11/02/2026', in_tracker=True)
                for n, a in (('5001', '100.00'), ('5002', '50.00'), ('5003', '70.00'), ('5004', '80.00'))]
        t = txn('11/05/2026', '150.00')
        by, _, _ = run([t], rows)
        self.assertEqual((by[t['id']]['status'], by[t['id']]['items']), ('ambiguous', []))

    def test_the_same_amount_far_in_time_is_not_the_same_money(self):
        t = txn('11/05/2026', '38.81')
        by, _, _ = run([t], [check('6001', '38.81', tracker_deposit='08/01/2026', in_tracker=True)])
        self.assertEqual(by[t['id']]['status'], 'unmatched')

    def test_a_date_difference_within_the_window_still_matches(self):
        t = txn('11/09/2026', '38.81')
        by, _, _ = run([t], [check('6002', '38.81', cashed_date='11/05/2026')])
        self.assertEqual((by[t['id']]['status'], by[t['id']]['rule']), ('matched · posted', 'amount and date'))

    def test_a_check_made_out_to_the_patient_is_never_helixonas_deposit(self):
        t = txn('11/05/2026', '38.81')
        by, _, _ = run([t], [check('6003', '38.81', cashed_date='11/05/2026', bs_bucket='cashed by patient')])
        self.assertEqual(by[t['id']]['status'], 'unmatched')

    def test_identical_amounts_pair_one_to_one_and_none_is_used_twice(self):
        rows = [check('7001', '50.00', cashed_date='11/03/2026'), check('7002', '50.00', cashed_date='11/04/2026')]
        a, b = txn('11/03/2026', '50.00', n=1), txn('11/04/2026', '50.00', n=2)
        by, _, _ = run([a, b], rows)
        self.assertEqual(sorted(by[a['id']]['items'] + by[b['id']]['items']), ['7001', '7002'])
        self.assertEqual((len(by[a['id']]['items']), len(by[b['id']]['items'])), (1, 1))

    def test_two_lines_one_check_matches_neither(self):
        a, b = txn('11/03/2026', '50.00', n=1), txn('11/04/2026', '50.00', n=2)
        by, _, _ = run([a, b], [check('7003', '50.00', cashed_date='11/03/2026')])
        self.assertEqual({by[a['id']]['status'], by[b['id']]['status']} & {'matched · posted'}, set())
        self.assertIn('ambiguous', (by[a['id']]['status'], by[b['id']]['status']))


class ElectronicPayments(unittest.TestCase):
    def test_the_trace_number_on_the_bank_line_is_the_835(self):
        rows = [{'check_number': '31401901', 'in_era': True, 'era_amount': '38.81', 'era_dated': '10/01/2026', 'verdict': '835 only',
                 'era_payer': 'Blue Shield of California'}]
        t = txn('11/05/2026', '38.81', desc='ACH HCCLAIMPMT BLUE SHIELD CA TRN*1*0031401901*')
        by, _, _ = run([t], rows)
        r = by[t['id']]
        self.assertEqual((r['status'], r['rule'], r['items'], r['variance']), ('matched · not posted', 'number on the bank line', ['31401901'], '38.81'))

    def test_a_number_with_another_amount_is_not_that_payment(self):
        rows = [{'check_number': '31401901', 'in_era': True, 'era_amount': '99.00', 'era_dated': '11/04/2026', 'verdict': '835 only'}]
        t = txn('11/05/2026', '38.81', desc='ACH TRN*1*31401901*')
        self.assertEqual(run([t], rows)[0][t['id']]['status'], 'unmatched')


class ReversalsAndTheRest(unittest.TestCase):
    def test_a_returned_item_reverses_its_credit_and_frees_the_check(self):
        credit = txn('11/03/2026', '250.00', n=1)
        back = txn('11/06/2026', '-250.00', desc='RETURNED DEPOSITED ITEM', n=2)
        by, _, sm = run([credit, back], [check('8001', '250.00', cashed_date='11/03/2026')])
        self.assertEqual((by[credit['id']]['status'], by[back['id']]['status']), ('reversed', 'reversal'))
        self.assertEqual(by[back['id']]['reverses'], credit['id'])
        self.assertEqual((sm['reversed'], sm['posted']), (1, 0))

    def test_card_settlements_are_out_of_scope_not_unmatched(self):
        t = txn('11/05/2026', '400.00', desc='BKCRD MERCH SETTLEMENT')
        by, _, sm = run([t], [])
        self.assertEqual((by[t['id']]['status'], sm['unmatched'], sm['other']), ('other income', 0, 1))

    def test_every_credit_is_in_exactly_one_bucket(self):
        rows = [check('9001', '10.00', cashed_date='11/03/2026'), check('9002', '20.00', verdict='unposted', cashed_date='11/03/2026')]
        txns = [txn('11/03/2026', '10.00', n=1), txn('11/03/2026', '20.00', n=2), txn('11/04/2026', '999.00', n=3),
                txn('11/04/2026', '5.00', desc='AMEX SETTLEMENT', n=4), txn('11/05/2026', '-7.00', desc='SERVICE FEE', n=5)]
        by, _, sm = run(txns, rows)
        self.assertTrue(sm['accounted'])
        self.assertEqual((sm['credits'], sm['posted'], sm['not_posted'], sm['unmatched'], sm['other']), (4, 1, 1, 1, 1))
        self.assertEqual((sm['credits_amount'], sm['variance']), ('1034.00', '1019.00'))
        used = [n for r in by.values() for n in r['items']]
        self.assertEqual(len(used), len(set(used)), 'no check behind two bank lines')


class WhatTheBankNeverShowed(unittest.TestCase):
    def test_a_recorded_deposit_with_no_credit_inside_the_statement_is_listed(self):
        rows = [check('1101', '300.00', in_tracker=True, tracker_deposit='11/04/2026')]
        txns = [txn('11/01/2026', '1.00', n=1), txn('11/15/2026', '2.00', n=2)]
        _, missing, sm = run(txns, rows)
        self.assertEqual([(m['what'], m['date'], m['amount'], m['items']) for m in missing],
                         [('tracker deposit', '2026-11-04', '300.00', ['1101'])])
        self.assertEqual((sm['not_in_bank'], sm['not_in_bank_amount']), (1, '300.00'))

    def test_a_deposit_outside_the_statements_dates_is_not_called_missing(self):
        rows = [check('1102', '300.00', in_tracker=True, tracker_deposit='11/14/2026')]
        _, missing, _ = run([txn('11/01/2026', '1.00', n=1), txn('11/15/2026', '2.00', n=2)], rows)
        self.assertEqual(missing, [], 'too close to the end of the statement to say')


class ReadOnlyAndReused(unittest.TestCase):
    def test_the_engine_touches_no_system(self):
        src = _read('src/checks/bank.py')
        for word in ('boto3', 'playwright', 'requests', 'anthropic', 'update_item', 'put_item'):
            self.assertNotIn(word, src)

    def test_it_is_built_on_the_remittance_rows_not_a_second_reconciliation(self):
        self.assertIn('from src.checks.reconcile import norm_check', _read('src/checks/bank.py'))


if __name__ == '__main__':
    unittest.main()


class _Tbl:
    def __init__(self, key, items=()):
        self.key, self.items, self.name = key, {i[key]: dict(i) for i in items}, ''

    def scan(self, **kw):
        return {'Items': [dict(i) for i in self.items.values()]}

    def put_item(self, Item):
        assert not any(isinstance(v, float) for v in Item.values()), 'DynamoDB takes no float'
        self.items[Item[self.key]] = dict(Item)

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        return {'Item': dict(it)} if it else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        row = self.items[Key[self.key]]
        for part in UpdateExpression.replace('SET ', '').split(', '):
            k, v = [x.strip() for x in part.split('=')]
            row[k] = ExpressionAttributeValues[v]


class TheDashboardPanel(unittest.TestCase):
    """Upload a statement, see it reconciled, record a person's review."""

    def setUp(self):
        import io
        from unittest import mock
        os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
        os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
        os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
        import dashboard
        self.io, self.d = io, dashboard
        self.bank = _Tbl('id')
        self.checks = _Tbl('check_number', [check('1001', '100.00', cashed_date='11/03/2026'),
                                            check('1002', '50.00', verdict='not in eCW', cashed_date='11/03/2026'),
                                            {'check_number': '_summary', 'reconciled_at': '2026-11-18 10:00:00 UTC'}])
        ddb = mock.Mock()
        ddb.Table.side_effect = lambda name: self.bank if name == dashboard.BANK_TABLE else self.checks
        ddb.tables.all.return_value = [mock.Mock()]
        ddb.tables.all.return_value[0].name = dashboard.BANK_TABLE
        for p in (mock.patch.object(dashboard, 'dynamodb', ddb), mock.patch.object(dashboard, 'S3_BUCKET', '')):
            p.start()
            self.addCleanup(p.stop)
        self.c = dashboard.app.test_client()

    CSV = b'Date,Description,Amount\n11/03/2026,REMOTE DEPOSIT,100.00\n11/04/2026,REMOTE DEPOSIT,50.00\n11/05/2026,DEPOSIT,777.00\n'

    def _upload(self, data=None, name='nov.csv'):
        return self.c.post('/api/bank/upload', data={'file': (self.io.BytesIO(data or self.CSV), name)}, content_type='multipart/form-data')

    def test_upload_reconcile_review(self):
        got = self._upload().get_json()
        self.assertEqual((got['transactions'], got['added']), (3, 3))
        # The same statement again adds nothing.
        self.assertEqual(self._upload().get_json()['added'], 0)
        data = self.c.get('/api/bank').get_json()
        by = {r['amount']: r for r in data['rows']}
        self.assertEqual((by['100.00']['status'], by['50.00']['status'], by['777.00']['status']),
                         ('matched · posted', 'matched · not posted', 'unmatched'))
        self.assertEqual((data['summary']['credits'], data['summary']['variance'], data['summary']['accounted']), (3, '827.00', True))
        self.assertEqual(data['summary']['last_upload']['file'], 'nov.csv')
        self.assertEqual(data['summary']['checks_reconciled_at'], '2026-11-18 10:00:00 UTC')
        # A person's word on the unexplained deposit; a later upload keeps it.
        tid = by['777.00']['id']
        self.assertEqual(self.c.post('/api/bank/review', json={'id': tid, 'by': 'Karina', 'note': 'patient cash, see ledger'}).get_json(), {'ok': True})
        self._upload()
        row = next(r for r in self.c.get('/api/bank').get_json()['rows'] if r['id'] == tid)
        self.assertEqual((row['resolved_by'], row['resolved_note'], row['status']), ('Karina', 'patient cash, see ledger', 'unmatched'))

    def test_a_review_needs_a_name_and_a_real_line(self):
        self._upload()
        self.assertEqual(self.c.post('/api/bank/review', json={'id': 'nope', 'by': 'K'}).status_code, 404)
        self.assertEqual(self.c.post('/api/bank/review', json={'id': 'x', 'by': ''}).status_code, 400)

    def test_a_file_that_is_not_a_statement_is_refused(self):
        r = self._upload(b'Name,Phone\nA,1\n', 'people.csv')
        self.assertEqual(r.status_code, 400)
        self.assertIn('no transactions found', r.get_json()['error'])

    def test_the_csv_lists_the_lines_and_what_the_bank_never_showed(self):
        self._upload()
        text = self.c.get('/api/bank.csv').get_data(as_text=True)
        self.assertIn('REMOTE DEPOSIT', text)
        self.assertIn('unmatched', text)

    def test_the_panel_lives_in_the_remittance_tab(self):
        html = self.d.DASHBOARD_HTML
        self.assertIn('id="bank-section" hidden', html)
        self.assertIn("if (bankSec) bankSec.hidden = (bot !== 'eob');", html)
