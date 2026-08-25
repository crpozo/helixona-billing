"""Opening ECW's Claim Status picker, and the date the claims search starts from.

The picker button was matched by a hardcoded id — button#billingClaimBtn83 —
whose numeric suffix is an index that varies per claim. It matched
occasionally. Every miss returned False and left the claim in its old status,
which is why claims recorded as updated in June still read "Ready to Bill -
Symplisend CC" in ECW in August, and why the resubmissions bot keeps re-walking
the same already-sent claims on every run.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, 'src', 'main.py')


def _read():
    with open(MAIN, encoding='utf-8') as fh:
        return fh.read()


def _step3():
    """The whole STEP 3 block, comments included."""
    src = _read()
    i = src.index('# STEP 3 — open the Claim Status Code picker')
    return src[i:src.index('# STEP 4', i)]


def _step3_js():
    """Only the JS that runs.

    The comment above it names the old hardcoded id, which would defeat an
    assertion about what the code tries first.
    """
    m = re.search(r"frm\.evaluate\('''(.+?)'''\)", _step3(), re.S)
    assert m, 'could not find the evaluate body'
    return m.group(1)


class ThePickerIsFoundByBehaviourNotById(unittest.TestCase):
    def test_the_hardcoded_index_is_gone(self):
        self.assertNotIn("querySelector('button#billingClaimBtn83')", _step3_js())

    def test_any_billing_claim_button_matches(self):
        self.assertIn('button[id^="billingClaimBtn"]', _step3_js())

    def test_the_ng_click_is_tried_first(self):
        js = _step3_js()
        self.assertIn('selectClaimStatusCode()', js)
        self.assertLess(js.index('selectClaimStatusCode()'), js.index('billingClaimBtn'))

    def test_it_falls_back_to_the_ellipsis_beside_the_field(self):
        js = _step3_js()
        self.assertIn(r'/claim\s*status/i', js)
        self.assertIn('ellipsis-near-label', js)

    def test_every_candidate_must_be_visible(self):
        self.assertIn('getBoundingClientRect', _step3_js())

    def test_the_failure_says_what_was_tried(self):
        self.assertIn('Tried selectClaimStatusCode, billingClaimBtn*', _read())


class AClaimAlreadyCorrectIsNotRewritten(unittest.TestCase):
    def test_it_returns_before_opening_the_picker(self):
        src = _read()
        i = src.index('Claim Status BEFORE')
        j = src.index('# STEP 3 — open the Claim Status Code picker')
        self.assertIn('return True', src[i:j])


class TheClaimsSearchStartsInJuly2025(unittest.TestCase):
    """The operator's ECW view runs from 07/01/2025; the bot must match it."""

    def test_no_june_start_date_remains(self):
        self.assertNotIn('06/01/2025', _read())

    def test_every_date_filter_uses_july_first(self):
        dates = set(re.findall(r"inp\.fill\('(\d{2}/\d{2}/\d{4})'\)", _read()))
        self.assertEqual(dates, {'07/01/2025'}, 'mixed start dates: %s' % dates)

    def test_the_saved_filter_records_the_same_date(self):
        self.assertIn("'filter_date_from': '07/01/2025'", _read())


if __name__ == '__main__':
    unittest.main()
