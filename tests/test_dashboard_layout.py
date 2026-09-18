"""The dashboard's layout per tab, and how fast it gets its data.

Adding the Remittance tab broke the page: `.main` is a two-column grid, and
with the EOB table, the claims table and the rail all visible, the claims
table was pushed into the 360px rail column and the rail wrapped below. The
same screenshot showed the Intake tab's claims under the Remittance tab,
because a tab switch waited for the next 3.2 MB fetch — uncompressed, ~50 s
over the network — instead of re-rendering what was already loaded.
"""
import gzip
import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dash():
    os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
    os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
    os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
    import dashboard
    return dashboard


def _src():
    with open(os.path.join(REPO, 'dashboard.py'), encoding='utf-8') as fh:
        return fh.read()


def _fn(name):
    src = _src()
    i = src.index(f'function {name}(')
    return src[i:src.index('\n        }\n', i)]


class EachTabShowsOneMainPanel(unittest.TestCase):
    def test_remittance_hides_the_claims_table(self):
        self.assertIn("claimsSec.hidden = (bot === 'eob')", _fn('setActiveBot'))

    def test_the_other_tabs_hide_the_checks_table(self):
        self.assertIn("chkSec.hidden = (bot !== 'eob')", _fn('setActiveBot'))

    def test_the_panels_are_pinned_to_their_columns(self):
        # Defense in depth: even with both panels visible, the rail keeps its column.
        src = _src()
        self.assertIn('.main > #checks-section, .main > #claims-section-submissions{grid-column:1}', src)
        self.assertIn('.main > .task-panel{grid-column:2;grid-row:1}', src)

    def test_single_column_screens_release_the_pin(self):
        # Otherwise grid-column:2 would conjure a second column on a laptop.
        src = _src()
        i = src.index('@media(max-width:1380px)')
        block = src[i:src.index('}\n@media', i)]
        self.assertIn('.main > .task-panel{grid-column:1;grid-row:auto}', block)


class ATabSwitchRendersAtOnce(unittest.TestCase):
    def test_it_renders_the_cached_claims_before_refetching(self):
        fn = _fn('setActiveBot')
        cached = fn.index('applyDateFilter(claimsForActiveBot(window._allClaims))')
        refetch = fn.index('loadData()')
        self.assertLess(cached, refetch)
        self.assertIn('renderClaims(cached)', fn)


class TheHeadlineSpeaksEachTabsLanguage(unittest.TestCase):
    def test_the_labels_are_addressable(self):
        src = _src()
        self.assertIn('id="hero-pct-label"', src)
        self.assertIn('id="hero-remaining-label"', src)

    def test_remittance_counts_matches_not_completions(self):
        fn = _fn('setActiveBot')
        self.assertIn("'match' : 'complete'", fn)
        self.assertIn("'do not match' : 'remaining'", fn)


class TheChecksTableIsTheRemittancePanel(unittest.TestCase):
    """The old per-claim EOB table and its posting plan are gone (2026-09-17):
    the bot compares checks, it does not enter payments."""
    def test_one_row_per_check_with_the_three_sources(self):
        fn = _fn('renderChecks')
        for col in ('copy_amount', 'bs_amount', 'bs_status', 'ecw_posted', 'ecw_unposted', 'verdict'):
            self.assertIn(col, fn)

    def test_the_old_eob_table_is_gone(self):
        src = _src()
        for gone in ('id="eob-section"', 'loadEobs', 'toggleEob', 'loadPlan', 'postCell', '/api/eobs',
                     'eob_capture', 'eob_post', 'with an EOB captured', 'Explanations of Benefits'):
            self.assertNotIn(gone, src, gone)

    def test_the_checks_stay_live_on_their_tab(self):
        self.assertIn("if (window.activeBot === 'eob') loadChecks();", _src())

    def test_the_empty_state_says_what_to_do(self):
        src = _src()
        self.assertIn('Reconcile checks</strong> from the task panel', src)
        self.assertIn('MFA code</strong> box', src)


class BigResponsesAreCompressed(unittest.TestCase):
    def _run(self, body, mimetype='application/json', accept='gzip, deflate'):
        d = _dash()
        headers = {'Accept-Encoding': accept} if accept else {}
        with d.app.test_request_context(headers=headers):
            resp = d.app.response_class(body, mimetype=mimetype)
            return d._gzip_large_responses(resp)

    def test_a_large_json_payload_is_gzipped_and_round_trips(self):
        body = json.dumps({'claims': [{'claim_id': str(i), 'patient_name': 'Doe, Jane'} for i in range(2000)]})
        out = self._run(body)
        self.assertEqual(out.headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(gzip.decompress(out.get_data()).decode(), body)
        self.assertLess(len(out.get_data()), len(body) / 5)

    def test_a_client_that_did_not_ask_gets_it_plain(self):
        out = self._run(json.dumps({'x': 'y' * 5000}), accept='')
        self.assertNotIn('Content-Encoding', out.headers)

    def test_small_responses_are_left_alone(self):
        out = self._run(json.dumps({'ok': True}))
        self.assertNotIn('Content-Encoding', out.headers)

    def test_other_types_are_left_alone(self):
        out = self._run('a,b\n' * 5000, mimetype='text/csv')
        self.assertNotIn('Content-Encoding', out.headers)


if __name__ == '__main__':
    unittest.main()
