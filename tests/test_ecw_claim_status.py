"""Changing a claim's status in ECW.

Three things had to be true at once and none of them were.

1. THE POPUP MUST BE THE RIGHT CLAIM. The old check asked whether the frame
   held buttons named Cancel/OK and one saying "Prog. Notes". The background
   claims-lookup screen has those, so a claim whose popup never opened was
   reported as opened.

2. THE STATUS MUST BE READ FROM THE POPUP. The read fell back to scanning the
   whole document for a span/td holding a known status, which matched the
   claim's row in the results grid — a plausible-looking value that says
   nothing about the popup, and could confirm a change that never happened.

3. BOTH RENDERINGS OF THE FIELD MUST BE DRIVEN. ECW shows the Claim Status
   either as a <select> bound to ClaimData.ClaimStatus (id claimStatusSel<ts>)
   or behind a "..." picker button. The submissions bot has been using the
   picker — 187 opens via billingClaimBtn83, the last on 2026-08-24 — so
   supporting only the <select> would have taken away the path that works for
   it.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, 'src', 'main.py')


def _read():
    with open(MAIN, encoding='utf-8') as fh:
        return fh.read()


def _fn(name):
    """One function's source, up to the next top-level def."""
    src = _read()
    i = src.index('def %s(' % name)
    return src[i:src.index('\ndef ', i + 10)]


def _popup_js():
    src = _read()
    m = re.search(r'POPUP_JS = r"""(.*?)"""', src, re.S)
    assert m, 'POPUP_JS not found'
    return m.group(1)


def _step3():
    body = _fn('_set_claim_status_in_ecw')
    return body[body.index('# STEP 3'):body.index('# STEP 4')]


def _step4():
    body = _fn('_set_claim_status_in_ecw')
    return body[body.index('# STEP 4'):body.index('# STEP 5')]


def _code(body):
    """A function with its docstring and comments stripped.

    Comments name the controls that used to be hunted for and the bugs that
    were fixed; either would defeat an assertion about what the code does.
    """
    if '"""' in body:
        a = body.index('"""')
        b = body.index('"""', a + 3) + 3
        body = body[:a] + body[b:]
    return '\n'.join(l for l in body.splitlines()
                     if not l.strip().startswith(('#', '//')))


class ThePopupMustBeTheRightClaim(unittest.TestCase):
    """A popup that never opened must not be reported as opened."""

    def test_the_finder_requires_the_claim_number(self):
        js = _popup_js()
        self.assertIn('claimId', js)
        self.assertIn(r'claim\\s*no', js)

    def test_the_finder_requires_popup_only_wording(self):
        # Claim numbers appear in the results grid too; "Print HCFA" and
        # "Prog. Notes" belong to the popup.
        js = _popup_js().lower()
        self.assertIn('print hcfa', js)
        self.assertIn('prog', js)

    def test_it_returns_the_smallest_matching_container(self):
        # The <body> contains the claim number too; the tightest box is the
        # one that is actually the popup.
        self.assertIn('best', _popup_js())

    def test_the_opener_verifies_identity_not_just_button_names(self):
        opener = _fn('_open_claim_popup_via_lookup')
        self.assertIn('_wait_for_claim_popup(page, claim_id', opener)
        self.assertIn('findClaimPopup', _fn('_wait_for_claim_popup'))

    def test_the_generic_button_name_check_is_gone(self):
        # (hasCancel || hasOK) && hasProgNotes matched the lookup screen.
        # Scoped to the verification step: the later best-frame scoring still
        # uses those words for a different purpose — ranking frames, not
        # deciding whether a popup exists.
        opener = _fn('_open_claim_popup_via_lookup')
        verify = opener[opener.index('# 2. Wait for'):opener.index('# 3. Find best frame')]
        self.assertNotIn('hasCancel', verify)
        self.assertNotIn('hasProgNotes', verify)
        self.assertIn('_wait_for_claim_popup', verify)

    def test_a_claim_whose_popup_did_not_open_is_reported_not_guessed(self):
        opener = _fn('_open_claim_popup_via_lookup')
        i = opener.index('_report_lookup_failure')
        self.assertIn('return (False, None)', opener[i:])


class TheStatusIsReadFromThePopupOnly(unittest.TestCase):
    """Reading the results grid produced a confident, meaningless answer."""

    def _read_block(self):
        body = _fn('_set_claim_status_in_ecw')
        return body[body.index('READ_STATUS_JS'):body.index('def _read_status')]

    def test_the_read_is_scoped_by_the_popup_finder(self):
        block = self._read_block()
        self.assertIn('_with_popup_finder', block)
        self.assertIn('findClaimPopup(claimId)', block)

    def test_no_popup_means_no_answer(self):
        self.assertIn('if (!popup) return null;', self._read_block())

    def test_nothing_is_read_from_the_whole_document(self):
        block = self._read_block()
        self.assertNotIn("document.querySelectorAll('span, td')", block)
        self.assertIn("popup.querySelectorAll('span, td')", block)

    def test_the_select_is_looked_for_inside_the_popup(self):
        self.assertIn('popup.querySelector(', self._read_block())

    def test_the_claim_id_is_passed_to_the_read(self):
        body = _fn('_set_claim_status_in_ecw')
        self.assertIn('frm.evaluate(READ_STATUS_JS, str(claim_id))', body)

    def test_the_text_fallback_knows_the_status_the_bot_extracts_on(self):
        # Omitting "Ready to Bill - Symplisend CC" made exactly the population
        # being fixed read back as None.
        block = self._read_block()
        self.assertIn("'ready to bill - symplisend cc'", block)
        self.assertIn("'claim sent via symplisend'", block)
        self.assertIn("'ready to submit to symplisend'", block)


class BothRenderingsOfTheFieldAreDriven(unittest.TestCase):
    def test_the_select_path_exists(self):
        self.assertIn('def _set_status_via_select(', _read())

    def test_the_picker_path_exists(self):
        # 187 successful opens on the submissions bot depend on it.
        self.assertIn('def _set_status_via_picker(', _read())

    def test_the_picker_still_knows_its_selectors(self):
        picker = _fn('_set_status_via_picker')
        self.assertIn('selectClaimStatusCode()', picker)
        self.assertIn('[id^="billingClaimBtn"]', picker)

    def test_the_picker_confirms_with_save_true_not_false(self):
        # The x close calls saveClaimStatusCodes(false) and discards the pick.
        picker = _fn('_set_status_via_picker')
        self.assertIn(r'saveClaimStatusCodes\(\s*true\s*\)', picker)
        self.assertIn(r'!/\(\s*false\s*\)/', picker)

    def test_both_paths_search_inside_the_popup(self):
        for name in ('_set_status_via_select', '_set_status_via_picker'):
            self.assertIn('findClaimPopup', _fn(name), name)
            self.assertIn('popup.querySelector', _fn(name), name)

    def test_neither_path_hunts_the_whole_document_for_its_control(self):
        # That is what matched the background lookup screen's own controls.
        picker = _code(_fn('_set_status_via_picker'))
        self.assertNotIn("document.querySelectorAll('[id^=\"billingClaimBtn\"]')", picker)

    def test_step3_tries_the_select_first_then_the_picker(self):
        step3 = _step3()
        self.assertLess(step3.index('_set_status_via_select'),
                        step3.index('_set_status_via_picker'))

    def test_a_failing_path_does_not_abort_the_other(self):
        # Each call is wrapped on its own, so a frame that throws on the
        # select does not skip the picker for that frame.
        step3 = _step3()
        loop = step3[step3.index('for attempt in range(1, 4):'):
                     step3.index('if status_set:')]
        self.assertEqual(loop.count('except Exception:'), 2)


class TheTargetOptionIsReadFromThePage(unittest.TestCase):
    """Selecting the wrong status is worse than failing to select one."""

    def test_the_option_code_is_not_hardcoded(self):
        code = _code(_fn('_set_status_via_select'))
        self.assertNotIn("'72CL'", code)
        self.assertNotIn('"72CL"', code)

    def test_the_match_is_exact_not_a_substring(self):
        self.assertIn(".trim().toLowerCase() === targetLc",
                      _fn('_set_status_via_select'))

    def test_no_matching_option_writes_nothing(self):
        sel = _fn('_set_status_via_select')
        self.assertIn('if not code:', sel)
        self.assertIn('return None', sel)

    def test_the_picker_matches_the_row_exactly_too(self):
        self.assertIn(".trim().toLowerCase() === targetLc",
                      _fn('_set_status_via_picker'))


class AngularIsToldTheValueChanged(unittest.TestCase):
    """A .value assignment alone leaves ng-model holding the old status."""

    def test_a_native_selection_is_preferred(self):
        self.assertIn('frm.select_option(select_css, value=code',
                      _fn('_set_status_via_select'))

    def test_the_fallback_dispatches_both_events(self):
        sel = _fn('_set_status_via_select')
        self.assertIn("new Event('input', {bubbles: true})", sel)
        self.assertIn("new Event('change', {bubbles: true})", sel)

    def test_the_fallback_applies_the_scope_without_double_applying(self):
        # $apply inside a digest throws; ng-change may already have started one.
        self.assertIn('$$phase', _fn('_set_status_via_select'))

    def test_the_fallback_confirms_the_value_took(self):
        self.assertIn('sel.value === code', _fn('_set_status_via_select'))


class EveryStackedAlertIsCleared(unittest.TestCase):
    """One OK is not enough.

    Claim 1931 carries "*Error (3)" and a "Suppressed Error" alongside the
    "No Master/Default fee schedule is selected." popup, and the claim form
    finishes binding only once the last one is gone.
    """

    def test_the_dismisser_loops_until_the_page_is_clear(self):
        block = _fn('_dismiss_claim_alert')
        self.assertIn('for _ in range(rounds)', block)
        self.assertIn('_dismiss_one_claim_alert(page)', block)

    def test_the_single_dismissal_is_still_available_separately(self):
        self.assertIn('def _dismiss_one_claim_alert(page):', _read())

    def test_the_fee_schedule_popup_is_recognised(self):
        self.assertIn('fee schedule is selected', _read())

    def test_the_wording_lives_in_one_place(self):
        src = _read()
        self.assertIn('CLAIM_ALERT_RE = (', src)
        self.assertEqual(src.count('fee schedule is selected|data loading error'), 1)
        self.assertIn('ALERT_RE = CLAIM_ALERT_RE', src)


class TheControlIsRetriedNotGivenUpOn(unittest.TestCase):
    def test_it_tries_three_times(self):
        self.assertIn('for attempt in range(1, 4):', _step3())

    def test_each_attempt_clears_alerts_first(self):
        step3 = _step3()
        i = step3.index('for attempt in range(1, 4):')
        j = step3.index('for frm in _all_frames(page):', i)
        self.assertIn('_dismiss_claim_alert(page)', step3[i:j])

    def test_it_waits_between_attempts(self):
        step3 = _step3()
        self.assertIn('if attempt < 3:', step3)
        self.assertIn('time.sleep(2.5)', step3)

    def test_a_success_stops_retrying(self):
        self.assertIn('if status_set:', _step3())

    def test_the_failure_says_it_gave_up_after_three(self):
        self.assertIn('after 3 attempts', _step3())


class AnAlertsOkIsNeverCountedAsASave(unittest.TestCase):
    """With an alert up, "click any visible OK" dismisses the alert and
    reports the claim as saved when nothing was written."""

    def test_alerts_are_cleared_before_looking_for_the_save_button(self):
        step4 = _step4()
        self.assertLess(step4.index('_dismiss_claim_alert(page)'),
                        step4.index('saved_via = None'))

    def test_both_ok_fallbacks_refuse_an_alert(self):
        self.assertEqual(_step4().count('!isAlert(b)'), 2)

    def test_the_alert_wording_drives_that_refusal(self):
        step4 = _step4()
        self.assertIn('CLAIM_ALERT_RE', step4)
        self.assertIn("new RegExp(alertRe, 'i')", step4)

    def test_the_claim_dialog_is_preferred_over_any_dialog(self):
        step4 = _step4()
        self.assertIn('isClaimDialog', step4)
        self.assertLess(step4.index('claimPopupOK'), step4.index('anyOK'))

    def test_the_save_control_may_be_any_tag(self):
        step4 = _step4()
        self.assertIn('[ng-click="saveAllData()"]', step4)
        self.assertNotIn('button[ng-click="saveAllData()"]', step4)
        self.assertNotIn('button[id^="claimScreenOkBtn"]', step4)

    def test_an_alert_raised_by_the_save_is_cleared_too(self):
        self.assertEqual(_step4().count('_dismiss_claim_alert(page)'), 2)


class AMissArrivesWithItsEvidence(unittest.TestCase):
    def test_it_reports_every_frame(self):
        self.assertIn('for n, frm in enumerate(_all_frames(page))', _step3())

    def test_it_says_whether_the_popup_is_even_there(self):
        # The question that three rounds of selector guessing never answered.
        step3 = _step3()
        self.assertIn('popup: !!popup', step3)
        self.assertIn('selectsInPopup', step3)

    def test_it_reports_both_kinds_of_control(self):
        step3 = _step3()
        self.assertIn('statusSelect', step3)
        self.assertIn('pickerButtons', step3)

    def test_it_names_the_frame_it_is_describing(self):
        self.assertIn('frm.url', _step3())

    def test_a_failure_closes_the_popup_and_reports_false(self):
        step3 = _step3()
        self.assertIn('_close_claim_popup(page)', step3)
        self.assertIn('return False', step3)


class AClaimAlreadyCorrectIsNotRewritten(unittest.TestCase):
    def test_it_returns_before_touching_the_field(self):
        body = _fn('_set_claim_status_in_ecw')
        i = body.index('Claim Status BEFORE')
        j = body.index('# STEP 3')
        self.assertIn('return True', body[i:j])


class OnlyAVerifiedChangeCounts(unittest.TestCase):
    """The in-popup value does not prove a save."""

    def test_the_claim_is_saved_after_the_field_changes(self):
        body = _code(_fn('_set_claim_status_in_ecw'))
        self.assertLess(body.index('_set_status_via_select'),
                        body.index('saveAllData'))

    def test_the_claim_is_re_opened_to_verify(self):
        body = _fn('_set_claim_status_in_ecw')
        i = body.index('# STEP 5 (verify)')
        self.assertIn('_open_claim_popup_via_lookup', body[i:])

    def test_true_is_returned_only_when_the_re_read_matches(self):
        body = _fn('_set_claim_status_in_ecw')
        self.assertIn("verified = (now or '').strip().lower() == target_lc", body)
        self.assertTrue(body.rstrip().endswith('return verified'))

    def test_an_unverified_claim_is_left_unmarked_for_retry(self):
        self.assertIn('leaving unmarked for retry', _fn('_set_claim_status_in_ecw'))


class TheClaimsSearchStartsInJuly2025(unittest.TestCase):
    """The operator's ECW view runs from 07/01/2025; the bot must match it."""

    def test_no_june_start_date_remains(self):
        self.assertNotIn('06/01/2025', _read())

    def test_every_date_filter_uses_july_first(self):
        dates = set(re.findall(r"inp\.fill\('(\d{2}/\d{2}/\d{4})'\)", _read()))
        self.assertEqual(dates, {'07/01/2025'}, 'mixed start dates: %s' % dates)

    def test_the_saved_filter_records_the_same_date(self):
        self.assertIn("'filter_date_from': '07/01/2025'", _read())


class TheClaimIsOpenedFromItsRow(unittest.TestCase):
    """The lookup box lists the claim; it does not open it.

    Until the row was clicked no popup was ever created, while the old
    detection reported that one had.
    """

    def test_there_is_a_row_opener(self):
        self.assertIn('def _open_claim_row(', _read())

    def test_the_column_is_the_measured_one_not_a_guess(self):
        # td[1] matched nothing, and so did the first two. The diagnostic
        # measured it: claimNumberInCell 6 of cellsInRow 16.
        fn = _fn('_open_claim_row')
        self.assertIn('//tr/td[position() <= 8][normalize-space()=', fn)
        self.assertNotIn('//tr[td[1]', fn)

    def test_a_wrong_candidate_cannot_be_acted_on(self):
        # Widening the column range is safe only because the popup has to show
        # THIS claim before anything is touched.
        fn = _fn('_open_claim_row')
        self.assertIn('_wait_for_claim_popup(page, claim_id', fn)

    def test_it_uses_real_mouse_input(self):
        # A dispatched MouseEvent('dblclick') is not trusted input; ECW ignored
        # it and every claim logged a click that opened nothing.
        fn = _fn('_open_claim_row')
        self.assertIn('cell.dblclick', fn)
        self.assertNotIn("new MouseEvent('dblclick'", fn)

    def test_double_click_is_tried_before_single_click(self):
        fn = _fn('_open_claim_row')
        self.assertLess(fn.index("'dblclick'"), fn.index("'click'"))

    def test_invisible_cells_are_skipped(self):
        self.assertIn('cell.is_visible()', _fn('_open_claim_row'))

    def test_it_confirms_the_popup_rather_than_assuming(self):
        fn = _fn('_open_claim_row')
        self.assertIn('_wait_for_claim_popup(page, claim_id', fn)
        self.assertIn('return True', fn)

    def test_a_quoted_claim_id_cannot_break_the_xpath(self):
        self.assertIn('.replace(\'"\', \'\')', _fn('_open_claim_row'))


class TheWaitForThePopupIsAPoll(unittest.TestCase):
    def test_there_is_a_polling_wait(self):
        self.assertIn('def _wait_for_claim_popup(', _read())

    def test_it_returns_as_soon_as_the_popup_is_up(self):
        fn = _fn('_wait_for_claim_popup')
        self.assertIn('return True', fn)
        self.assertIn('deadline', fn)

    def test_the_fixed_sleep_is_gone(self):
        opener = _fn('_open_claim_popup_via_lookup')
        self.assertNotIn('_time.sleep(wait_seconds)', opener)

    def test_the_row_is_only_opened_when_the_popup_is_not_already_up(self):
        opener = _fn('_open_claim_popup_via_lookup')
        self.assertIn('if not popup_found:', opener)
        self.assertIn('popup_found = _open_claim_row(page, claim_id)', opener)


class ALookupMissExplainsItself(unittest.TestCase):
    def test_the_failure_is_reported_before_giving_up(self):
        opener = _fn('_open_claim_popup_via_lookup')
        i = opener.index('_report_lookup_failure(page, claim_id)')
        self.assertIn('return (False, None)', opener[i:])

    def test_it_says_whether_the_grid_even_holds_this_claim(self):
        # The question that separates "the search failed" from "the row would
        # not open" — two different next moves.
        fn = _fn('_report_lookup_failure')
        self.assertIn('rowForThisClaim', fn)
        self.assertIn('lookupValue', fn)


class ThePopupIsFoundHoweverBigItIs(unittest.TestCase):
    """A size limit threw away the answer.

    findClaimPopup skipped any container longer than 6000 characters. A claim
    popup carrying six ICD codes, fifteen CPT lines, insurances and the
    Follow-up panel is far bigger, so the element that actually matched was
    skipped and a claim the operator could SEE open on screen read as "not
    found" — after which every search for a status control looked at the
    background lookup screen.
    """

    def test_there_is_no_size_limit(self):
        js = _popup_js()
        self.assertNotIn('6000', js)
        self.assertNotIn('t.length >', js)

    def test_the_smallest_match_still_wins(self):
        # What makes the limit unnecessary: <body> matches too, and loses.
        js = _popup_js()
        self.assertIn('best', js)
        self.assertIn('<', js)

    def test_identity_and_popup_wording_are_still_required(self):
        js = _popup_js()
        self.assertIn('idRe.test(t)', js)
        self.assertIn('print hcfa', js.lower())


class EveryTabIsSearched(unittest.TestCase):
    """ECW can open a claim in a second tab.

    page.frames only covers the tab the search ran in, so a popup plainly on
    screen is invisible to anything scoped to one page.
    """

    def test_there_is_an_all_tabs_helper(self):
        self.assertIn('def _all_frames(page):', _read())

    def test_it_walks_the_browser_context(self):
        fn = _fn('_all_frames')
        self.assertIn('page.context.pages', fn)

    def test_it_still_includes_the_page_it_was_given(self):
        # A context that reports no pages must not silently search nothing.
        fn = _fn('_all_frames')
        self.assertIn('pages.insert(0, page)', fn)

    def test_it_does_not_return_the_same_frame_twice(self):
        self.assertIn('seen', _fn('_all_frames'))

    def test_the_status_flow_uses_it_everywhere(self):
        src = _read()
        flow = src[src.index('def _all_frames'):src.index('def _combine_excels')]
        # The docstring mentions page.frames; no loop should still use it.
        self.assertNotIn('in page.frames:', flow)
        self.assertGreaterEqual(flow.count('_all_frames(page)'), 6)


class TheClaimIsGivenTimeToRender(unittest.TestCase):
    """It opened at about five seconds; the bot stopped looking at 4.4.

    The run reported "Could not find claim 7149" while the operator watched
    that claim open on screen.
    """

    def test_the_first_wait_is_not_three_seconds(self):
        sig = "def _open_claim_popup_via_lookup(page, claim_id, wait_seconds="
        line = _read()[_read().index(sig):]
        line = line[:line.index(')')]
        self.assertNotIn('wait_seconds=3', line)
        self.assertIn('wait_seconds=12', line)

    def test_a_clicked_row_gets_twelve_seconds(self):
        self.assertIn('_wait_for_claim_popup(page, claim_id, 12)',
                      _fn('_open_claim_row'))

    def test_there_is_a_last_look_before_giving_up(self):
        # The claim can finish rendering while the row search runs.
        opener = _fn('_open_claim_popup_via_lookup')
        i = opener.index('_open_claim_row(page, claim_id)')
        j = opener.index('_report_lookup_failure')
        self.assertIn('_wait_for_claim_popup(page, claim_id, 6)', opener[i:j])


class TheDiagnosticSeparatesTheTwoBugs(unittest.TestCase):
    """"No popup opened" and "a popup is open and the matcher misses it" have
    looked identical in every run so far."""

    def test_it_reports_whether_popup_wording_is_on_screen(self):
        self.assertIn('popupWording', _fn('_report_lookup_failure'))

    def test_it_reports_which_claim_the_screen_is_showing(self):
        self.assertIn('claimNoShown', _fn('_report_lookup_failure'))

    def test_it_reports_which_column_holds_the_claim_number(self):
        # Assuming the first column is exactly what cost the last run.
        fn = _fn('_report_lookup_failure')
        self.assertIn('claimNumberInCell', fn)
        self.assertIn('cellsInRow', fn)


class TheFinderReachesThePage(unittest.TestCase):
    """findClaimPopup never ran, and never said so.

    POPUP_JS is a function DECLARATION. Concatenated in front of an arrow
    function it became

        function findClaimPopup(claimId) {...}
        ((claimId) => ...)

    which Playwright parenthesises, making it a CALL of findClaimPopup with the
    arrow function where the claim id belongs. It did not throw — it returned
    null, Playwright passed that back as the result, null is falsy, and every
    caller read "no popup". Silently, on every claim, from the day it was
    written, while the popup was open on screen.
    """

    def test_the_broken_concatenation_is_gone(self):
        self.assertNotIn('POPUP_JS + ', _read())

    def test_the_declaration_is_wrapped_inside_the_called_function(self):
        fn = _fn('_with_popup_finder')
        self.assertIn("'((...args) => {%s", fn)
        self.assertIn('.apply(null, args)', fn)

    def test_the_wrapper_yields_one_expression(self):
        # What went wrong: two statements where one expression was required.
        fn = _fn('_with_popup_finder')
        self.assertTrue(fn.rstrip().endswith("% (POPUP_JS, arrow_src)"), fn[-80:])

    def test_every_use_of_the_finder_goes_through_the_wrapper(self):
        src = _read()
        # Any evaluate mentioning findClaimPopup must be wrapped.
        for chunk in src.split('frm.evaluate(')[1:]:
            head = chunk[:400]
            if 'findClaimPopup' in head:
                self.assertIn('_with_popup_finder', head[:60], head[:120])


if __name__ == '__main__':
    unittest.main()
