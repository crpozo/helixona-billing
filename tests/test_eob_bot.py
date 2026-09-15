"""Remittance — the EOB bot: what it reads from Blue Shield, and what it may not do.

The recording of 2026-09-07 is the specification. The portal's claim-status
results are read BY HEADER NAME because the Finalized view has an extra "EOB"
column and moves Check/EFT to the end — positional parsing, which the old
In-process scraper used, silently reads the wrong field.
"""
import os
import unittest

from src.eob.parse import (
    dos_start, header_key, index_claims, match_claim, money,
    parse_check_summary, parse_eob_pdf_text, rows_by_header, rows_with_links,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _repo_only(rel):
    """infra/ and the setup scripts are deliberately not deployed; on the host
    these checks have nothing to read and must skip rather than error."""
    if not os.path.exists(os.path.join(REPO, rel)):
        raise unittest.SkipTest(f'{rel} is not deployed to this host')
    return _read(rel)


# The Finalized results table, columns exactly as the portal shows them.
FINALIZED_HEADERS = [
    'Claim status (Last modified)', 'Claim number', 'Claim type', 'Dates of service',
    'EOB', 'Member name', 'Member ID / Subscriber ID', 'Provider name',
    'Claim amount billed', 'Claim amount paid', 'Patient responsibility', 'Check/EFT number',
]
FINALIZED_ROW = [
    'FINALIZED 09/07/2026', '260703051601 (adjusted)', 'Medical', '01/23/2026–01/23/2026',
    'View EOB', 'GRAY, CASSANDRA', '909681878', 'HELIXONA INC',
    '$1,113.00', '$263.67', '$850.31', '30979207',
]
# The In-process view, where Check/EFT sits fourth. Same parser, same answer.
INPROCESS_HEADERS = [
    'Claim status', 'Claim number', 'Claim type', 'Dates of service', 'Check/EFT number',
    'Member name', 'Member ID / Subscriber ID', 'Provider name',
    'Claim amount billed', 'Claim amount paid', 'Patient responsibility',
]


class ResultsAreReadByHeaderName(unittest.TestCase):
    def test_the_finalized_layout_maps_every_field(self):
        r = rows_by_header(FINALIZED_HEADERS, [FINALIZED_ROW])[0]
        self.assertEqual(r['bsc_claim_number'], '260703051601')
        self.assertEqual(r['claim_note'], 'adjusted')
        self.assertEqual(r['check_eft'], '30979207')
        self.assertEqual(r['subscriber_id'], '909681878')
        self.assertEqual(r['amount_paid'], '$263.67')
        self.assertEqual(r['patient_resp'], '$850.31')

    def test_column_order_does_not_matter(self):
        row = ['IN PROCESS', '260703051601', 'Medical', '01/23/2026–01/23/2026', '30979207',
               'GRAY, CASSANDRA', '909681878', 'HELIXONA INC', '$1,113.00', '$0.00', '$0.00']
        r = rows_by_header(INPROCESS_HEADERS, [row])[0]
        self.assertEqual(r['check_eft'], '30979207')
        self.assertEqual(r['amount_paid'], '$0.00')

    def test_the_cheque_cell_carries_a_label_with_the_number(self):
        # The screen, 2026-09-15: '30912971 Check/EFT information'. The link
        # text is the same. The number is what we keep.
        row = ['FINALIZED 09/14/2026', '260308710901 (adjusted)', 'Medical', '01/02/2026–01/02/2026',
               'View EOB', 'MARTINEZ, KARINA E', '914555917', 'HELIXONA INC', '$1,156.00', '$275.09',
               '$581.25', '30912971 Check/EFT information']
        r = rows_by_header(FINALIZED_HEADERS, [row])[0]
        self.assertEqual(r['check_eft'], '30912971')
        self.assertEqual(r['check_eft_text'], '30912971 Check/EFT information')
        self.assertEqual(r['bsc_claim_number'], '260308710901')
        self.assertEqual(r['claim_note'], 'adjusted')
        raw = [{'cells': row, 'links': [{'text': '260308710901', 'href': ''}, {'text': 'View EOB', 'href': ''},
                                        {'text': 'MARTINEZ, KARINA E', 'href': ''},
                                        {'text': '30912971 Check/EFT information', 'href': 'https://p/k'}]}]
        r = rows_with_links(FINALIZED_HEADERS, raw)[0]
        self.assertEqual(r['check_eft'], '30912971')
        self.assertEqual(r['check_href'], 'https://p/k')

    def test_an_unnamed_column_does_not_shift_the_values(self):
        # A select-all checkbox column has a cell in every row and no header
        # text. 2026-09-15: fifty rows read, not one cheque number among them.
        r = rows_by_header(FINALIZED_HEADERS, [[''] + FINALIZED_ROW])[0]
        self.assertEqual(r['check_eft'], '30979207')
        self.assertEqual(r['amount_paid'], '$263.67')
        self.assertEqual(r['column_shift'], 1)
        # And when the empty header IS reported, nothing needs sliding.
        r = rows_by_header([''] + FINALIZED_HEADERS, [[''] + FINALIZED_ROW])[0]
        self.assertEqual(r['check_eft'], '30979207')
        self.assertNotIn('column_shift', r)

    def test_a_row_whose_claim_cell_is_not_a_number_is_not_a_claim(self):
        row = ['FINALIZED 09/07/2026', 'not a number', 'Medical', '01/23/2026', 'View EOB',
               'GRAY', '909681878', 'HELIXONA', '$1.00', '$1.00', '$0.00', '30979207']
        self.assertEqual(rows_by_header(FINALIZED_HEADERS, [row]), [])

    def test_the_cheque_link_is_the_fallback_for_the_cheque_number(self):
        # A layout whose cheque column is named something new: the number is
        # still the row's one numeric link that is not the claim number.
        hdrs = [h if h != 'Check/EFT number' else 'Payment reference' for h in FINALIZED_HEADERS]
        raw = [{'cells': FINALIZED_ROW,
                'links': [{'text': '260703051601', 'href': 'https://p/claim'},
                          {'text': 'View EOB', 'href': 'https://p/eob'},
                          {'text': '30979207', 'href': 'https://p/check'}]}]
        r = rows_with_links(hdrs, raw)[0]
        self.assertEqual(r['check_eft'], '30979207')
        self.assertEqual(r['check_from'], 'link')
        self.assertEqual(r['check_href'], 'https://p/check')
        self.assertEqual(r['eob_href'], 'https://p/eob')

    def test_rows_without_a_claim_number_are_not_claims(self):
        self.assertEqual(rows_by_header(FINALIZED_HEADERS, [['(adjusted)', '', '']]), [])

    def test_unknown_headers_are_ignored_not_fatal(self):
        self.assertIsNone(header_key('Something new the portal added'))
        r = rows_by_header(['Claim number', 'Mystery'], [['260703051601', 'x']])
        self.assertEqual(r[0], {'bsc_claim_number': '260703051601'})

    def test_the_export_columns_are_read_including_our_account_number(self):
        # The portal's CSV export, columns exactly as seen on 2026-09-15.
        hdrs = ['Claim status', 'Claim status date', 'Claim number', 'Claim type', 'Date received',
                'Dates of service', 'Member name', 'Subscriber ID', 'Patient account number',
                'Provider name', 'Claim amount billed', 'Allowed amount', 'Deductible amount',
                'Co-Insurance amount', 'Claim amount paid', 'Payment date', 'Patient responsibility',
                'Check/EFT number', 'Check/EFT date', 'Check/EFT amount', 'Check status', 'ACH number',
                'ACH amount', 'Payee name', 'Payee address']
        row = ['FINALIZED', '09/11/2026', '260703051601', 'Medical', '01/25/2026', '01/23/2026-01/23/2026',
               'GRAY, CASSANDRA', '909681878', '2627', 'HELIXONA INC', '$1,113.00', '$437.82', '$0.00',
               '$175.13', '$262.69', '09/11/2026', '$850.31', '31401901', '09/11/2026', '$38.81',
               'Check Number Assigned', '', '', 'HELIXONA INC', '114 Pacifica']
        r = rows_by_header(hdrs, [row])[0]
        self.assertEqual(r['patient_account_number'], '2627')
        self.assertEqual(r['check_eft'], '31401901')
        self.assertEqual(r['check_date'], '09/11/2026')
        self.assertEqual(r['check_amount'], '$38.81')
        self.assertEqual(r['check_status'], 'Check Number Assigned')
        self.assertEqual(r['allowed'], '$437.82')

    def test_the_export_account_number_pins_the_claim(self):
        c = _read('src/eob/capture.py')
        self.assertIn("exported = norm_text(r.get('patient_account_number'))", c)
        self.assertIn("acct_by_bsc.get(bsc) or exported or match_claim(r, claim_idx)", c)
        self.assertIn("'export_account_number'", c)

    def test_the_check_details_claims_table_uses_the_same_reader(self):
        hdrs = ['Row #', 'Subscriber ID', 'Member name', 'Claim #', 'Dates of service',
                'Date finalized', 'Claim amount billed', 'Claim amount paid']
        r = rows_by_header(hdrs, [['1', '909681878', 'CASSANDRA GRAY', '260703051601',
                                   '01/23/2026 - 01/23/2026', '09/07/2026', '$1,113.00', '$263.67']])[0]
        self.assertEqual(r['date_finalized'], '09/07/2026')
        self.assertEqual(r['bsc_claim_number'], '260703051601')


class MoneyAndDatesAreExact(unittest.TestCase):
    def test_money_keeps_cents_as_text(self):
        # Floats would turn $263.67 into 263.66999…; the ledger wants cents.
        self.assertEqual(money('$1,113.00'), '1113.00')
        self.assertEqual(money('$263.67'), '263.67')
        self.assertEqual(money('-$12.50'), '-12.50')
        self.assertEqual(money(''), '')

    def test_dos_start_takes_the_first_date_of_a_range(self):
        self.assertEqual(dos_start('01/23/2026–01/23/2026'), '01/23/2026')
        self.assertEqual(dos_start('10/17/2025 - 10/19/2025'), '10/17/2025')
        self.assertEqual(dos_start(''), '')


class ClaimsArePinnedOnlyWhenCertain(unittest.TestCase):
    """An EOB pinned to the wrong claim would post a payment against the
    wrong service. No guessing between candidates."""

    CLAIMS = [
        {'claim_id': '2627', 'subscriber_id': '909681878', 'service_date': '01/23/2026', 'charges': '1113.00'},
        {'claim_id': '2628', 'subscriber_id': '909681878', 'service_date': '01/23/2026', 'charges': '250.00'},
        {'claim_id': '11', 'subscriber_id': '914555917', 'service_date': '07/22/2025', 'charges': '452.00'},
    ]

    def test_a_unique_subscriber_and_dos_matches(self):
        idx = index_claims(self.CLAIMS)
        row = {'subscriber_id': '914555917', 'dos': '07/22/2025–07/22/2025', 'amount_billed': '$452.00'}
        self.assertEqual(match_claim(row, idx), '11')

    def test_two_claims_on_one_day_are_told_apart_by_billed_amount(self):
        idx = index_claims(self.CLAIMS)
        row = {'subscriber_id': '909681878', 'dos': '01/23/2026–01/23/2026', 'amount_billed': '$250.00'}
        self.assertEqual(match_claim(row, idx), '2628')

    def test_an_unresolvable_tie_matches_nothing(self):
        idx = index_claims(self.CLAIMS)
        row = {'subscriber_id': '909681878', 'dos': '01/23/2026–01/23/2026', 'amount_billed': '$999.00'}
        self.assertEqual(match_claim(row, idx), '')

    def test_the_portals_member_suffix_does_not_hide_a_match(self):
        # The portal shows "909151452-01"; our record says 909151452.
        idx = index_claims([{'claim_id': '77', 'subscriber_id': '909151452', 'service_date': '02/24/2026', 'charges': '643.50'}])
        row = {'subscriber_id': '909151452-01', 'dos': '02/24/2026–02/24/2026', 'amount_billed': '$643.50'}
        self.assertEqual(match_claim(row, idx), '77')

    def test_a_stranger_matches_nothing(self):
        idx = index_claims(self.CLAIMS)
        self.assertEqual(match_claim({'subscriber_id': 'X', 'dos': '01/01/2020'}, idx), '')


class TheCheckSummaryIsReadFromItsLabels(unittest.TestCase):
    PAGE = ("Check/EFT details Back to search results Check/EFT transaction summary "
            "Download EOB report Check/EFT # Check/EFT amount Check/EFT date Check/EFT status "
            "30979207 $263.67 05/05/2026 Check Cashed "
            "Check cashed date Payee name Payee address Number of claims "
            "05/14/2026 CASSANDRA GRAY 17 Hallcrest Dr Ladera Ranch CA 92694-1085 1 "
            "Claims information Row # Subscriber ID")

    def test_every_field_lands(self):
        s = parse_check_summary(self.PAGE)
        self.assertEqual(s['check_eft'], '30979207')
        self.assertEqual(s['check_amount'], '263.67')
        self.assertEqual(s['check_date'], '05/05/2026')
        self.assertEqual(s['cashed_date'], '05/14/2026')
        self.assertEqual(s['num_claims'], '1')
        self.assertIn('CASSANDRA GRAY', s['payee_name'])

    def test_a_page_without_the_summary_yields_blanks_not_errors(self):
        s = parse_check_summary('Tools & resources for providers')
        self.assertEqual(s['check_amount'], '')
        self.assertEqual(s['check_date'], '')


class TheEobPdfGivesUpTheAccountNumber(unittest.TestCase):
    """The PATIENT ACCOUNT NUMBER on the EOB is the eCW claim number — the
    one thing that pins a payer's claim to ours with no guessing."""

    TEXT = ("HELIXONA INC 114 PACIFICA STE 150 IRVINE, CA 92618 ISSUE DATE: 05 05 26 "
            "EOB NUMBER: 26125B10001420245467 PREFERRED PROVIDER NO PROVIDER NUMBER: PG0139141001 "
            "EXPLANATION OF BENEFITS THIS IS NOT A BILL "
            "PATIENT NAME PATIENT ACCOUNT NUMBER CLAIM NUMBER "
            "CASSANDRA GRAY 2627 260703051601 "
            "01/23/26 96375 1 250.00 153.65 96.35 0.00 61.54 92.11 "
            "01/23/26 J3490 5 25.00 3.31 21.69 0.00 1.15 2.16 "
            "TOTALS: 1113.00 437.82 0.00 175.13 262.69 "
            "RECAPITULATION OF STATEMENT SUMMARY TOTALS: APPROVE-TO-PAY: $262.69 "
            "INTEREST PAYMENTS: 0.98 OFFSETS TAKEN: 0.00 CHECK AMOUNT: $263.67")

    def test_eob_number_and_totals(self):
        p = parse_eob_pdf_text(self.TEXT)
        self.assertEqual(p['eob_number'], '26125B10001420245467')
        self.assertEqual(p['approve_to_pay'], '262.69')
        self.assertEqual(p['check_amount'], '263.67')
        self.assertTrue(p['parsed_ok'])

    def test_account_number_pairs_with_the_payers_claim_number(self):
        p = parse_eob_pdf_text(self.TEXT)
        self.assertEqual(p['claims'][0]['patient_account_number'], '2627')
        self.assertEqual(p['claims'][0]['bsc_claim_number'], '260703051601')

    def test_cpt_lines_are_captured_best_effort(self):
        p = parse_eob_pdf_text(self.TEXT)
        cpts = [l['cpt'] for l in p.get('lines', [])]
        self.assertIn('96375', cpts)
        self.assertIn('J3490', cpts)

    def test_garbage_in_is_not_parsed_ok(self):
        self.assertFalse(parse_eob_pdf_text('')['parsed_ok'])
        self.assertFalse(parse_eob_pdf_text('EXPLANATION OF BENEFITS')['parsed_ok'])


class TheRoleIsFencedBothWays(unittest.TestCase):
    def test_the_eob_bot_only_runs_eob_tasks(self):
        src = _read('src/main.py')
        self.assertIn("EOB_TASKS = {'eob_capture', 'eob_post'}", src)
        self.assertIn("_settings.bot_role == 'eob' and task_type not in EOB_TASKS", src)

    def test_the_other_bots_never_run_eob_tasks(self):
        self.assertIn("_settings.bot_role != 'eob' and task_type in EOB_TASKS", _read('src/main.py'))

    def test_the_iv_guard_is_untouched(self):
        # tests/test_ecw_status_filter.py pins its exact wording; keep it.
        self.assertIn("_settings.bot_role != 'iv_corrections' and task_type in IV_CORRECTIONS_TASKS",
                      _read('src/main.py'))


class OneLoginSharedByEveryBot(unittest.TestCase):
    """Two inline copies existed; a third for Remittance was the point of no return."""

    def test_the_submissions_flow_calls_the_shared_login(self):
        src = _read('src/main.py')
        i = src.index("elif task_type == 'blueshield_submissions':")
        block = src[i:src.index('Navigating to SympliSend', i)]
        self.assertIn('login_to_provider_portal(page, aws_client)', block)
        # The inline copy is gone from this handler.
        self.assertNotIn("page.click('text=Send code'", block)
        self.assertNotIn('pf.challengeResponse', block)

    def test_the_eob_bot_uses_it_too(self):
        self.assertIn('login_to_provider_portal(page, aws_client)', _read('src/eob/capture.py'))

    def test_forms_are_submitted_through_the_prototype(self):
        # Blue Shield's pages carry <input name="submit">, which shadows
        # form.submit() — "form.submit is not a function" killed a capture.
        s = _read('src/blueshield/session.py')
        self.assertNotIn('form.submit();', s)
        self.assertNotIn('f.submit();', s)
        self.assertIn('HTMLFormElement.prototype.submit.call(form)', s)
        self.assertNotIn('?.submit()', _read('src/main.py'))

    def test_the_shared_login_keeps_the_whole_sequence(self):
        s = _read('src/blueshield/session.py')
        for step in ('pf.username', 'Send code', 'fetch_mfa_code', 'Trust/Remember page detected',
                     'LOGIN SUCCESS — Blue Shield Portal', 'return False', 'return True'):
            self.assertIn(step, s, step)


class TheCaptureIsSafeToRepeat(unittest.TestCase):
    def test_a_cheque_already_on_file_with_its_pdf_is_skipped(self):
        c = _read('src/eob/capture.py')
        self.assertIn("done.get(ck) and not force", c)

    def test_it_never_writes_to_ecw(self):
        c = _read('src/eob/capture.py')
        for forbidden in ('_set_claim_status_in_ecw', 'Payment Advisory', 'saveAllData', 'ecwcloud'):
            self.assertNotIn(forbidden, c, forbidden)

    def test_only_a_real_pdf_is_kept(self):
        self.assertIn("data[:5] == b'%PDF-'", _read('src/eob/capture.py'))

    def test_the_operators_filters_are_the_defaults(self):
        c = _read('src/eob/capture.py')
        self.assertIn("_mat_select(page, 'Claim status', 'Finalized')", c)
        self.assertIn("'Payment information', 'Claim amount paid'", c)
        self.assertIn("'0.01'", c)
        self.assertIn("DEFAULT_SINCE = '07/01/2025'", c)

    def test_the_next_cheque_is_opened_from_the_results_not_the_details_page(self):
        # Export rows carry no href; after one details page the results are
        # gone, and clicking the next link there timed out on every cheque but
        # the first (2026-09-15).
        c = _read('src/eob/capture.py')
        self.assertIn('def _open_check_link(page, check):', c)
        self.assertIn('Back to search results', c)
        self.assertIn("page.go_back(", c)
        self.assertLess(c.index('Back to search results'), c.index('page.click(link, timeout=10000)'))
        # The results page itself is never left: back would drop the search.
        self.assertIn("on_details = ", c)
        self.assertIn('_open_check_link(page, check)', c[c.index('def _capture_check('):])

    def test_results_are_walked_on_screen_page_by_page_not_exported(self):
        # The operator's call (2026-09-15): the export did not line up with the
        # screen. Cheques are read off the results as shown, "Show more claims"
        # at a time, and each one is saved as soon as it is captured.
        c = _read('src/eob/capture.py')
        self.assertNotIn('_try_export', c)
        self.assertNotIn("page.click('text=Export'", c)
        self.assertIn('def _show_more(page):', c)
        self.assertIn('def _back_to_results(page):', c)
        run = c[c.index('def run_eob_capture('):]
        self.assertIn('_capture_check(page, aws_client, ck, info[\'href\'], info[\'rows\'], claim_idx, known_pdfs)', run)
        self.assertIn("done[ck] = True", run)
        self.assertIn('results page {pages + 1}', run)

    def test_the_table_reader_keeps_empty_headers_and_reads_one_table(self):
        c = _read('src/eob/capture.py')
        js = c[c.index('TABLE_JS = r"""'):c.index('def _shot(')]
        self.assertNotIn('.filter(Boolean)', js)
        self.assertIn("querySelectorAll('table, mat-table, [role=\"table\"], [role=\"grid\"]')", js)
        self.assertIn("querySelectorAll('a, button')", js)

    def test_a_layout_without_cheque_numbers_is_reported_not_paged(self):
        c = _read('src/eob/capture.py')
        run = c[c.index('def run_eob_capture('):]
        self.assertIn("logger.info(f\"  columns: {hdrs}\")", run)
        self.assertIn("_shot(page, 'results_no_checks')", run)
        self.assertLess(run.index("_shot(page, 'results_no_checks')"), run.index('for ck in new:'))

    def test_the_pdf_link_opens_in_the_browser(self):
        d = _read('dashboard.py')
        self.assertIn("'ResponseContentType': 'application/pdf'", d)
        self.assertIn("'ResponseContentDisposition': f'inline; filename=\"eob_{check_eft}.pdf\"'", d)

    def test_the_cheque_is_always_opened_by_clicking_its_link(self):
        # Every cheque's href is the same /claims/checkeftDetails; the app
        # picks the cheque from the click. Navigating to the href shows nothing.
        c = _read('src/eob/capture.py')
        body = c[c.index('def _capture_check('):c.index('def run_eob_capture(')]
        self.assertIn('_open_check_link(page, check)', body)
        self.assertNotIn('page.goto(href', body)

    def test_a_miss_leaves_a_screenshot(self):
        self.assertIn("_shot(page, 'results_unparsed')", _read('src/eob/capture.py'))


class RemittanceHasItsPlaceOnTheDashboard(unittest.TestCase):
    def _dash(self):
        os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
        os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
        os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
        import dashboard
        return dashboard

    def test_every_bot_has_a_name(self):
        d = self._dash()
        self.assertEqual({k: v['name'] for k, v in d.BOT_ROUTING.items()},
                         {'submissions': 'Intake', 'resubmissions': 'Follow-up', 'eob': 'Remittance'})

    def test_the_names_are_on_the_tabs(self):
        html = self._dash().DASHBOARD_HTML
        for label in ('📋 Intake', '🩺 Follow-up', '🧾 Remittance'):
            self.assertIn(label, html)

    def test_marea_routes_to_its_own_unit_queue_and_screen(self):
        r = self._dash().BOT_ROUTING['eob']
        self.assertEqual(r['service'], 'helixona-agent-eob')
        self.assertEqual(r['novnc_port'], 6083)

    def test_marea_owns_its_task_and_no_submission_task(self):
        import re
        html = self._dash().DASHBOARD_HTML
        m = re.search(r'<option value="eob_capture" data-bot="([^"]+)"', html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).split(), ['eob'])
        m = re.search(r'<option value="blueshield_submissions" data-bot="([^"]+)"', html)
        self.assertNotIn('eob', m.group(1).split())

    def test_the_headline_changes_meaning_on_the_eob_tab(self):
        html = self._dash().DASHBOARD_HTML
        self.assertIn('id="hero-denom-label"', html)
        self.assertIn("'submitted claims with an EOB captured'", html)

    def test_the_novnc_map_comes_from_the_server(self):
        html = self._dash().DASHBOARD_HTML
        self.assertIn('const BOT_NOVNC = {{ bot_novnc | tojson }}', html)
        self.assertNotIn('const BOT_NOVNC = {submissions: 6080', html)

    def test_the_eob_endpoints_exist(self):
        src = _read('dashboard.py')
        self.assertIn("@app.route('/api/eobs')", src)
        self.assertIn("@app.route('/api/eob/<check_eft>/pdf')", src)

    def test_the_counts_gain_an_eob_key_without_touching_the_partition(self):
        src = _read('dashboard.py')
        self.assertIn("'eob': {'submitted': len(with_eob), 'total': len(sent)}", src)


class TheHostSideIsWiredToo(unittest.TestCase):
    def test_the_unit_file_exists_on_its_own_display(self):
        u = _repo_only('infra/helixona-agent-eob.service')
        self.assertIn('Environment=DISPLAY=:102', u)
        self.assertIn('Environment=BOT_ROLE=eob', u)
        self.assertIn('Remittance', u)

    def test_the_deploy_script_restarts_the_bots_that_exist(self):
        d = _read('deploy_code.sh')
        for unit in ('helixona-agent', 'helixona-agent-resub', 'helixona-agent-eob', 'helixona-dashboard'):
            self.assertIn(f'systemctl restart {unit}', d)
        # The retired IV bot must not be re-enabled by every deploy.
        self.assertNotIn('systemctl enable helixona-agent-iv', d)

    def test_the_eob_table_is_declared(self):
        self.assertIn('"TableName": "helixona-eobs"', _repo_only('setup_dynamodb.py'))


if __name__ == '__main__':
    unittest.main()
