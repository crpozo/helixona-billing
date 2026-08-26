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
    def test_a_failure_reports_every_frame_not_just_the_first(self):
        """Breaking on the first frame reported the wrong screen.

        The diagnostic stopped at the first frame containing any <select> and
        printed the background claims-lookup controls — claimLookupSel1,
        patient-lookupSel16 — while the frame holding the claim popup was never
        looked at. It read as "the claim has no status field" when the real
        answer was "you did not look there".
        """
        step3 = _step3()
        self.assertIn('for n, frm in enumerate(page.frames)', step3)
        self.assertIn('claim-status select present', step3)
        self.assertIn('ngModel', step3)

    def test_the_report_names_the_frame_it_is_describing(self):
        self.assertIn('frm.url', _step3())

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


class EveryStackedAlertIsCleared(unittest.TestCase):
    """One OK is not enough.

    Claim 1931 carries "*Error (3)" and a "Suppressed Error" alongside the
    "No Master/Default fee schedule is selected." popup. The helper dismissed a
    single alert and returned, leaving the rest on top of the claim form — and
    the form only finishes binding once the last one is gone, so the Claim
    Status <select> was still absent when STEP 3 looked for it.
    """

    def test_the_dismisser_loops_until_the_page_is_clear(self):
        src = _read()
        i = src.index('def _dismiss_claim_alert(page, rounds=4)')
        block = src[i:src.index('\ndef ', i + 10)]
        self.assertIn('for _ in range(rounds)', block)
        self.assertIn('_dismiss_one_claim_alert(page)', block)

    def test_the_single_dismissal_is_still_available_separately(self):
        self.assertIn('def _dismiss_one_claim_alert(page):', _read())

    def test_the_fee_schedule_popup_is_recognised(self):
        # The exact wording eCW shows on the claim in question.
        self.assertIn('fee schedule is selected', _read())

    def test_the_wording_lives_in_one_place(self):
        # The save step needs the same list to tell an alert's OK from the
        # claim's OK; two copies of it would drift.
        src = _read()
        self.assertIn('CLAIM_ALERT_RE = (', src)
        # The pattern itself, not the prose mentions of it in comments.
        self.assertEqual(src.count('fee schedule is selected|data loading error'), 1)
        # And both users reference the constant rather than re-spelling it.
        self.assertIn('ALERT_RE = CLAIM_ALERT_RE', src)


class TheSelectIsRetriedNotGivenUpOn(unittest.TestCase):
    def test_it_tries_three_times(self):
        self.assertIn('for attempt in range(1, 4):', _step3())

    def test_each_attempt_clears_alerts_first(self):
        step3 = _step3()
        i = step3.index('for attempt in range(1, 4):')
        j = step3.index('for frm in page.frames:', i)
        self.assertIn('_dismiss_claim_alert(page)', step3[i:j])

    def test_it_waits_between_attempts(self):
        step3 = _step3()
        self.assertIn('if attempt < 3:', step3)
        self.assertIn('time.sleep(2.5)', step3)

    def test_a_success_stops_retrying(self):
        step3 = _step3()
        self.assertIn('if status_set:', step3)

    def test_the_failure_says_it_gave_up_after_three(self):
        self.assertIn('after 3 attempts', _step3())


class AnAlertsOkIsNeverCountedAsASave(unittest.TestCase):
    """The last-resort "click any visible OK" is the dangerous one.

    With an alert on screen it dismisses the alert and reports the claim as
    saved, when nothing was written.
    """

    def _step4(self):
        body = _func()
        return body[body.index('# STEP 4'):body.index('# STEP 5')]

    def test_alerts_are_cleared_before_looking_for_the_save_button(self):
        step4 = self._step4()
        i = step4.index('_dismiss_claim_alert(page)')
        self.assertLess(i, step4.index('saved_via = None'))

    def test_both_ok_fallbacks_refuse_an_alert(self):
        step4 = self._step4()
        self.assertEqual(step4.count('!isAlert(b)'), 2)

    def test_the_alert_wording_drives_that_refusal(self):
        step4 = self._step4()
        self.assertIn('CLAIM_ALERT_RE', step4)
        self.assertIn("new RegExp(alertRe, 'i')", step4)

    def test_the_claim_dialog_is_preferred_over_any_dialog(self):
        step4 = self._step4()
        self.assertIn('isClaimDialog', step4)
        self.assertLess(step4.index('claimPopupOK'), step4.index('anyOK'))

    def test_the_save_control_may_be_any_tag(self):
        # ECW renders these as button, input and a; a button-only selector
        # missed the claim popup's OK entirely.
        step4 = self._step4()
        self.assertIn('[ng-click="saveAllData()"]', step4)
        self.assertNotIn('button[ng-click="saveAllData()"]', step4)
        self.assertNotIn('button[id^="claimScreenOkBtn"]', step4)

    def test_an_alert_raised_by_the_save_is_cleared_too(self):
        # Left up, it blocks the re-open that STEP 5 verifies with.
        step4 = self._step4()
        self.assertEqual(step4.count('_dismiss_claim_alert(page)'), 2)


if __name__ == '__main__':
    unittest.main()
