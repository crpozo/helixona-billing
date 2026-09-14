"""Remittance → eCW: the gate on the two writes, and the wiring around them.

docs/ecw_posting.md: clicking Payment Advisory SAVES the payment. So a dry
run must fill the Payments popup and stop, and only "post": true may click
it. These tests pin that order in the code, and the pure helpers around it.
"""
import os
import unittest

from src.eob.post import locate_rows

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class TheDryRunStopsBeforeTheSave(unittest.TestCase):
    def setUp(self):
        self.p = _read('src/eob/post.py')
        self.body = self.p[self.p.index('def post_cheque('):]

    def test_post_false_returns_before_payment_advisory_is_clicked(self):
        i = self.body.index("if not post:")
        j = self.body.index("['Payment Advisory']")
        self.assertLess(i, j)
        self.assertIn("'status': 'dry_run'", self.body[i:j])
        self.assertIn("cancel_popups(page)", self.body[i:j])

    def test_the_gate_is_the_task_body(self):
        self.assertIn("post = bool(body.get('post'))", self.p)

    def test_the_two_writes_are_in_the_operators_order(self):
        adv = self.body.index("['Payment Advisory']")
        go = self.body.index("open_advisory_for_claim(page, cid)")
        auto = self.body.index("['Auto Post', 'Auto Post (F2)']")
        self.assertLess(adv, go)
        self.assertLess(go, auto)
        # Post Payment (F4) is never used (docs/ecw_posting.md).
        self.assertNotIn("Post Payment", self.p)

    def test_nothing_is_typed_until_every_row_is_located(self):
        loc = self.body.index("located, why = locate_rows(")
        typ = self.body.index("type_grid(page, frm, cols, located)")
        self.assertLess(loc, typ)
        self.assertIn("if located is None:", self.body[loc:typ])

    def test_an_incomplete_payment_is_reported_with_its_id(self):
        self.assertIn("'status': 'incomplete'", self.body)
        self.assertIn("finish or delete it in eCW", self.body)

    def test_only_ready_plans_reach_ecw(self):
        run = self.p[self.p.index('def run_eob_post('):]
        self.assertIn("if plan['status'] != 'ready':", run)
        self.assertLess(run.index("plan_from_item(it, **readers)"), run.index("if not login(page, creds, aws_client):"))


class TheValuesComeFromThePlanNotFromHere(unittest.TestCase):
    def test_the_popup_and_grid_values_are_decided_in_ecw_payment(self):
        p = _read('src/eob/post.py')
        self.assertIn('from src.eob.ecw_payment import grid_rows, payment_header, posting_total', p)
        self.assertIn('from src.eob.plan import plan_from_item', p)
        # No drug-table or matching logic of its own.
        self.assertNotIn('DRUG_TABLE', p)
        self.assertNotIn('counterpart_amounts', p)

    def test_the_popup_follows_the_recording(self):
        p = _read('src/eob/post.py')
        for step in ("'Single Ins Payment'", "'Get Insurance'", "PAYER_RADIO = 'Blue Shield of California'",
                     "what='Check No.'", "what='Amount $'", "what='EOB Date'", "what='Deposit Date'",
                     "['Go', 'Go (F3)']"):
            self.assertIn(step, p, step)
        self.assertIn("DEFAULT_SINCE = '07/01/2025'", p)


class GridRowsArePinnedBeforeTyping(unittest.TestCase):
    GRID = [
        {'idx': 0, 'code': '96365', 'billed': '325.00', 'inputs': set()},
        {'idx': 1, 'code': 'J3490', 'billed': '900.00', 'inputs': set()},
        {'idx': 2, 'code': 'J3490', 'billed': '125.00', 'inputs': set()},
        {'idx': 3, 'code': 'J3490', 'billed': '40.00', 'inputs': set()},
    ]

    def _p(self, i, cpt, billed):
        return {'ecw_index': i, 'cpt': cpt, 'ecw_billed': billed, 'values': {}}

    def test_the_plans_position_is_taken_when_it_carries_the_plans_line(self):
        rows, why = locate_rows(self.GRID, [self._p(1, 'J3490', '900.00'), self._p(3, 'J3490', '40.00')])
        self.assertEqual(why, '')
        self.assertEqual([r['idx'] for r, _ in rows], [1, 3])

    def test_a_shifted_grid_is_recovered_by_code_and_billed_when_unique(self):
        rows, why = locate_rows(self.GRID, [self._p(0, 'J3490', '125.00')])
        self.assertEqual(why, '')
        self.assertEqual(rows[0][0]['idx'], 2)

    def test_two_identical_rows_are_never_guessed(self):
        grid = self.GRID + [{'idx': 4, 'code': 'J3490', 'billed': '40.00', 'inputs': set()}]
        rows, why = locate_rows(grid, [self._p(9, 'J3490', '40.00')])
        self.assertIsNone(rows)
        self.assertIn('not typed', why)

    def test_a_row_is_used_once(self):
        rows, why = locate_rows(self.GRID, [self._p(3, 'J3490', '40.00'), self._p(3, 'J3490', '40.00')])
        self.assertIsNone(rows)


class TheBotAndDashboardAreWired(unittest.TestCase):
    def test_the_bot_runs_it_without_a_proxy_and_with_the_shared_ecw_login(self):
        src = _read('src/main.py')
        i = src.index("elif task_type == 'eob_post':")
        block = src[i:i + 1400]
        self.assertIn('BrowserManager().start(proxy_config=None)', block)
        self.assertIn('run_eob_post(page, aws_client, body, login=_perform_ecw_login)', block)

    def test_the_capture_still_never_touches_ecw(self):
        c = _read('src/eob/capture.py')
        self.assertNotIn('Payment Advisory', c)
        self.assertNotIn('from src.eob.post', c)

    def test_the_dashboard_offers_it_to_remittance_only_and_defaults_to_dry_run(self):
        import re
        d = _read('dashboard.py')
        m = re.search(r'<option value="eob_post" data-bot="([^"]+)"', d)
        self.assertEqual(m.group(1).split(), ['eob'])
        i = d.index('eob_post: JSON.stringify({')
        self.assertIn('post: false', d[i:i + 200])

    def test_the_ecw_column_shows_the_last_attempt(self):
        d = _read('dashboard.py')
        self.assertIn("function postCell(e, esc)", d)
        self.assertIn("INCOMPLETE — finish in eCW", d)


class TheLiveScreenIsOnTheDashboard(unittest.TestCase):
    def test_the_panel_follows_the_active_bot(self):
        d = _read('dashboard.py')
        self.assertIn('id="live-screen-frame"', d)
        self.assertIn('view_only=true', d)
        self.assertIn('const port = BOT_NOVNC[bot] || 6080;', d)
        self.assertIn("if (typeof syncLiveScreen === 'function') syncLiveScreen();", d)


if __name__ == '__main__':
    unittest.main()
