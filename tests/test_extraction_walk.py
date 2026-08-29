"""Which claims the documentation extraction actually walks.

The extraction re-walked hundreds of already-finished claims on every run —
a ~7 second popup each — because their "needs" never emptied: an office visit
never gets an encounter_date, and a failed encounter capture stays flagged
forever. The operator watched it "abrir claims ya existentes" for two hours.
"""
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _walk():
    with open(os.path.join(REPO, 'src', 'main.py'), encoding='utf-8') as fh:
        src = fh.read()
    i = src.index('elif claim_state >= 2:')
    return src[i:src.index('claims to process', i)]


class ASubmittedCompleteClaimIsNotWalked(unittest.TestCase):
    def test_it_is_skipped_before_any_needs_are_computed(self):
        w = _walk()
        i = w.index("item.get('symplisend_submitted')")
        self.assertLess(i, w.index('needs_hcfa_redo = False'))
        self.assertIn('continue', w[i:i + 700])

    def test_complete_means_the_documents_that_are_actually_sent(self):
        w = _walk()
        i = w.index("item.get('symplisend_submitted')")
        block = w[i:i + 400]
        self.assertIn('has_hcfa', block)
        self.assertIn('has_prog_notes', block)
        self.assertIn("item.get('subscriber_id')", block)

    def test_a_wrong_patient_note_still_disqualifies(self):
        # Submitted with a mismatched IV note is not "complete"; it must keep
        # being surfaced, not silently skipped.
        w = _walk()
        i = w.index("item.get('symplisend_submitted')")
        self.assertIn("iv_note_patient_mismatch", w[i:i + 400])

    def test_the_skip_says_so_in_the_log(self):
        self.assertIn('nothing to collect', _walk())


class AnOfficeVisitHasNoEncounterToChase(unittest.TestCase):
    """An empty encounter_date is its normal, permanent state — requiring one
    re-walked every office visit on every extraction, forever."""

    def test_encounter_date_is_not_required_for_office_visits(self):
        self.assertIn("needs_enc_date = not _is_office and not item.get('encounter_date')",
                      _walk())

    def test_encounter_file_is_not_required_for_office_visits(self):
        self.assertIn('needs_enc_file = not _is_office and (', _walk())

    def test_iv_claims_still_get_their_encounter_hunted(self):
        # The exemption is the office visit, not the encounter logic.
        w = _walk()
        self.assertIn("not item.get('encounter_file_s3_path')", w)
        self.assertIn("encounter_revision_needed", w)


class TheHcfaPdfIsFetchedInsideThePage(unittest.TestCase):
    """The download failed on every claim, for two structural reasons.

    A synchronous XHR may not set responseType — the old code threw "The
    response type cannot be changed for synchronous requests" on every single
    claim — and any client outside the browser gets a 400 from Cloudflare no
    matter how many session cookies it carries. Only an in-page async fetch,
    riding the browser's own session, can download this PDF.
    """

    def _dl(self):
        with open(os.path.join(REPO, 'src', 'main.py'), encoding='utf-8') as fh:
            src = fh.read()
        i = src.index('Downloading PDF via iframe XHR')
        return src[i:src.index('Method C', i)]

    def test_no_synchronous_xhr_remains(self):
        dl = self._dl()
        self.assertNotIn("xhr.open('GET', url, false)", dl)
        self.assertNotIn('new XMLHttpRequest()', dl)

    def test_the_fetch_is_async_and_in_page(self):
        dl = self._dl()
        self.assertIn("fetch(url, { credentials: 'include' })", dl)
        self.assertIn('hcfa_context.evaluate', dl)

    def test_a_non_ok_response_reports_its_status(self):
        self.assertIn('status: r.status', self._dl())

    def test_only_a_real_pdf_is_saved(self):
        # A 400 page or an HTML render must never be stored as the HCFA.
        self.assertIn("pdf_bytes[:5] == b'%PDF-'", self._dl())

    def test_the_requests_fallback_stays_for_the_day_cloudflare_relents(self):
        self.assertIn('import requests as _requests', self._dl())


if __name__ == '__main__':
    unittest.main()
