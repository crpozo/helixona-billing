"""Which SympliSend form each claim goes through.

803 resubmissions went to the payer on the first-submission form, so Blue
Shield opened new claims instead of attaching the documents to the ones under
dispute. Every one of them had the prior claim number sitting on its record.
These tests pin the routing that prevents a repeat.
"""
import ast
import os
import unittest

from src.symplisend.form_types import (
    ATTACHMENT_BS_REQUESTED,
    FIRST_SUBMISSION,
    PRIOR_CLAIM,
    describe,
    form_for,
    is_resubmission,
    prior_claim_number,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, 'src', 'main.py')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


RESUB = {'claim_id': '2196', 'submission_type': 'Resubmission',
         'original_ref_no': '260308710901', 'subscriber_id': 'XEA914555917'}
FIRST = {'claim_id': '7540', 'submission_type': 'First Time Submission',
         'subscriber_id': 'XED913009672'}


class RoutingByClaimType(unittest.TestCase):
    def test_a_resubmission_goes_to_the_prior_claim_form(self):
        self.assertEqual(form_for(RESUB), PRIOR_CLAIM)

    def test_a_first_time_claim_goes_to_the_first_submission_form(self):
        self.assertEqual(form_for(FIRST), FIRST_SUBMISSION)

    def test_the_two_forms_are_different(self):
        self.assertNotEqual(PRIOR_CLAIM, FIRST_SUBMISSION)

    def test_the_form_names_match_the_payer_ui_exactly(self):
        # Matched against the navigation text; a near-miss selects nothing.
        self.assertEqual(PRIOR_CLAIM, 'Provider Prior Claim Submission')
        self.assertEqual(FIRST_SUBMISSION, 'Provider First Submission Claim')

    def test_classification_is_case_insensitive(self):
        for label in ('Resubmission', 'RESUBMISSION', 'resubmission'):
            self.assertTrue(is_resubmission({'submission_type': label}), label)

    def test_an_unclassified_claim_is_not_a_resubmission(self):
        self.assertFalse(is_resubmission({}))
        self.assertFalse(is_resubmission({'submission_type': ''}))


class ThePriorClaimNumberTravels(unittest.TestCase):
    """The field whose absence caused the whole problem."""

    def test_it_comes_from_the_claim_record(self):
        self.assertEqual(describe(RESUB)['claim_number'], '260308710901')

    def test_it_is_stripped(self):
        self.assertEqual(
            prior_claim_number({'original_ref_no': '  260308710901 '}),
            '260308710901')

    def test_a_first_submission_carries_none(self):
        self.assertEqual(describe(FIRST)['claim_number'], '')

    def test_attachment_type_is_what_the_payer_asked_for(self):
        self.assertEqual(describe(RESUB)['attachment_type'], ATTACHMENT_BS_REQUESTED)

    def test_the_first_submission_form_has_no_attachment_type(self):
        self.assertEqual(describe(FIRST)['attachment_type'], '')

    def test_medicare_and_heat_default_to_no(self):
        d = describe(RESUB)
        self.assertFalse(d['is_medicare'])
        self.assertFalse(d['is_heat_claim'])


class AResubmissionWithoutItsNumber(unittest.TestCase):
    """The prior-claim form requires the number, so it cannot be used."""

    CLAIM = {'submission_type': 'Resubmission', 'original_ref_no': '',
             'subscriber_id': 'ABC123'}

    def test_it_falls_back_to_a_first_submission(self):
        self.assertEqual(form_for(self.CLAIM), FIRST_SUBMISSION)

    def test_the_fallback_is_flagged_not_silent(self):
        # This is the old behaviour for every resubmission. It must never pass
        # unrecorded again.
        self.assertTrue(describe(self.CLAIM)['downgraded'])

    def test_a_routed_resubmission_is_not_flagged(self):
        self.assertFalse(describe(RESUB)['downgraded'])


class TheSubmissionStepUsesIt(unittest.TestCase):
    def test_main_asks_the_mapping_which_form_to_open(self):
        src = _read(MAIN)
        self.assertIn('form_types.describe(claim_item)', src)

    def test_main_no_longer_hardcodes_the_first_submission_form(self):
        src = _read(MAIN)
        i = src.index('Choose the right SympliSend form')
        j = src.index('Enter Subscriber ID using page-level', i)
        self.assertNotIn("const target = 'Provider First Submission Claim'", src[i:j])

    def test_a_missing_claim_number_blocks_the_send(self):
        # Submitting anyway would reproduce the exact defect.
        src = _read(MAIN)
        self.assertIn('skipping rather than sending it as a new claim', src)
        self.assertIn('OUTCOME_BLOCKED', src)

    def test_it_still_parses(self):
        ast.parse(_read(MAIN))


if __name__ == '__main__':
    unittest.main()
