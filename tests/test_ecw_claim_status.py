"""Changing a claim's status in ECW.

The Claim Status field is a plain <select> bound to Angular's
ClaimData.ClaimStatus:

    <select ng-model="ClaimData.ClaimStatus"
            id="claimStatusSel1787764786180"
            ng-change="ClaimStautsChange('ChangeEvent')">
      <option value="72CL">Claim sent via Symplisend</option>
      <option value="68RE" selected>Ready to Bill - Symplisend CC</option>

Every earlier version of this step tried to OPEN a lookup picker — first
button#billingClaimBtn83, then any billingClaimBtn*, then an ellipsis beside
the label. No such control exists on this screen, so the step failed on every
claim, not on some. Claims recorded as updated in June still read "Ready to
Bill - Symplisend CC" in August, and the resubmissions bot re-walks those same
already-sent claims on every run.

These tests hold the shape of the real control so the picker cannot come back.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, 'src', 'main.py')


def _read():
    with open(MAIN, encoding='utf-8') as fh:
        return fh.read()


def _func():
    """The whole _set_claim_status_in_ecw function."""
    src = _read()
    i = src.index('def _set_claim_status_in_ecw')
    return src[i:src.index('\ndef _combine_excels', i)]


def _code():
    """The function with its docstring and Python comments removed.

    Both kinds of comment name the controls that used to be hunted for, and the
    docstring names the save step — either would defeat an assertion about what
    the code actually does.
    """
    body = _func()
    a = body.index('"""')
    b = body.index('"""', a + 3) + 3
    body = body[:a] + body[b:]
    return '\n'.join(l for l in body.splitlines()
                     if not l.strip().startswith(('#', '//')))


def _step3():
    body = _func()
    return body[body.index('# STEP 3'):body.index('# STEP 4')]


class TheStatusFieldIsASelectNotAPicker(unittest.TestCase):
    def test_nothing_tries_to_open_a_lookup_picker(self):
        code = _code()
        for gone in ('selectClaimStatusCode', 'billingClaimBtn',
                     'saveClaimStatusCodes', 'ellipsis-near-label'):
            self.assertNotIn(gone, code, f'{gone} is a picker that does not exist')

    def test_the_select_is_matched_by_its_angular_binding(self):
        self.assertIn('select[ng-model="ClaimData.ClaimStatus"]', _code())

    def test_the_data_prefixed_binding_is_accepted_too(self):
        self.assertIn('select[data-ng-model="ClaimData.ClaimStatus"]', _code())

    def test_the_id_is_matched_by_prefix_only(self):
        # claimStatusSel1787764786180 — the suffix is a per-render timestamp,
        # so a whole-id match works once and never again.
        code = _code()
        self.assertIn('select[id^="claimStatusSel"]', code)
        self.assertFalse(re.search(r'claimStatusSel\d', code),
                         'a timestamped id is hardcoded')


class TheTargetOptionIsReadFromThePage(unittest.TestCase):
    """Selecting the wrong status is worse than failing to select one."""

    def test_the_option_code_is_not_hardcoded(self):
        # '72CL' is today's code for "Claim sent via Symplisend". Hardcoding it
        # means a re-coded list silently sets some other status.
        self.assertNotIn("'72CL'", _code())
        self.assertNotIn('"72CL"', _code())

    def test_the_code_is_looked_up_by_the_label_asked_for(self):
        step3 = _step3()
        self.assertIn('targetLc', step3)
        self.assertIn('o.value', step3)

    def test_the_match_is_exact_not_a_substring(self):
        # "Claim sent via Symplisend" must not match some longer option.
        self.assertIn(".trim().toLowerCase() === targetLc", _step3())

    def test_a_claim_with_no_matching_option_is_not_written(self):
        step3 = _step3()
        self.assertIn('if not code:', step3)
        self.assertIn('continue', step3)


class AngularIsToldTheValueChanged(unittest.TestCase):
    """A .value assignment alone leaves ng-model holding the old status."""

    def test_a_native_selection_is_preferred(self):
        self.assertIn('select_option(SELECT_CSS, value=code', _step3())

    def test_the_fallback_dispatches_both_events(self):
        step3 = _step3()
        self.assertIn("new Event('input', {bubbles: true})", step3)
        self.assertIn("new Event('change', {bubbles: true})", step3)

    def test_the_fallback_applies_the_scope_without_double_applying(self):
        # $apply inside a digest throws; ng-change may already have started one.
        self.assertIn('$$phase', _step3())

    def test_the_fallback_confirms_the_value_took(self):
        self.assertIn('sel.value === code', _step3())


class TheStatusIsReadFromTheSelect(unittest.TestCase):
    def test_the_read_prefers_the_selected_option(self):
        read = _func()[:_func().index('def _read_status')]
        self.assertIn('sel.options[sel.selectedIndex]', read)

    def test_the_text_fallback_knows_the_status_the_bot_extracts_on(self):
        # Omitting it made claims sitting in "Ready to Bill - Symplisend CC"
        # read back as None, which is exactly the population being fixed.
        self.assertIn("'ready to bill - symplisend cc'", _func())

    def test_it_still_recognises_the_target_and_the_submission_status(self):
        func = _func()
        self.assertIn("'claim sent via symplisend'", func)
        self.assertIn("'ready to submit to symplisend'", func)


class AClaimAlreadyCorrectIsNotRewritten(unittest.TestCase):
    def test_it_returns_before_touching_the_select(self):
        body = _func()
        i = body.index('Claim Status BEFORE')
        j = body.index('# STEP 3')
        self.assertIn('return True', body[i:j])


class AMissArrivesWithItsEvidence(unittest.TestCase):
    def test_a_failure_reports_the_selects_that_do_exist(self):
        step3 = _step3()
        self.assertIn('selects on the page', step3)
        self.assertIn('ngModel', step3)

    def test_a_failure_closes_the_popup_and_reports_false(self):
        step3 = _step3()
        self.assertIn('_close_claim_popup(page)', step3)
        self.assertIn('return False', step3)


class OnlyAVerifiedChangeCounts(unittest.TestCase):
    """The in-popup value does not prove a save."""

    def test_the_claim_is_saved_after_the_select_changes(self):
        code = _code()
        self.assertLess(code.index('select_option'), code.index('saveAllData'))

    def test_the_claim_is_re_opened_to_verify(self):
        body = _func()
        i = body.index('# STEP 5 (verify)')
        self.assertIn('_open_claim_popup_via_lookup', body[i:])

    def test_true_is_returned_only_when_the_re_read_matches(self):
        body = _func()
        self.assertIn("verified = (now or '').strip().lower() == target_lc", body)
        self.assertTrue(body.rstrip().endswith('return verified'))

    def test_an_unverified_claim_is_left_unmarked_for_retry(self):
        self.assertIn('leaving unmarked for retry', _func())


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
