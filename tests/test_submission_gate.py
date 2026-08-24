"""The rule that decides what documentation reaches a payer.

Every assertion here is a promise to a patient: their records only leave when
the packet is complete, belongs to them, and has not gone already. A failure in
this file should stop a deploy.
"""
import unittest

from src.rules.submission_gate import (
    evaluate_claim,
    is_office_visit,
    ready_to_submit,
)


def claim(**overrides):
    """An IV-therapy claim with complete, verified documentation."""
    base = {
        'claim_id': '1234',
        'patient_name': 'Doe, Jane',
        'cpt': '96365, J3490',
        'hcfa_s3_path': 's3://bucket/hcfa_forms/1234_hcfa.pdf',
        'prog_notes_s3_path': 's3://bucket/prog_notes/1234_prog_notes.pdf',
        'encounter_file_s3_path': 's3://bucket/encounter_files/1234_encounter_1.pdf',
        'subscriber_id': 'XEA914555915',
        'subscriber_id_unverified': 0,
        'iv_note_patient_mismatch': 0,
        'encounter_revision_needed': 0,
        'symplisend_submitted': 0,
    }
    base.update(overrides)
    return base


class CompleteClaimPasses(unittest.TestCase):
    def test_fully_documented_iv_claim_is_ready(self):
        self.assertTrue(ready_to_submit(claim()))
        self.assertEqual(evaluate_claim(claim())['blockers'], [])


class MissingDocumentsBlock(unittest.TestCase):
    """A packet is never sent incomplete."""

    def test_no_hcfa_blocks(self):
        v = evaluate_claim(claim(hcfa_s3_path=''))
        self.assertFalse(v['ready'])
        self.assertIn('HCFA', v['blockers'])

    def test_no_iv_note_blocks(self):
        v = evaluate_claim(claim(prog_notes_s3_path=''))
        self.assertFalse(v['ready'])
        self.assertIn('IV Note', v['blockers'])

    def test_missing_field_entirely_is_same_as_empty(self):
        c = claim()
        del c['hcfa_s3_path']
        self.assertFalse(evaluate_claim(c)['ready'])


class QualityFlagsBlock(unittest.TestCase):
    """Paths can be present while the content is known to be wrong."""

    def test_iv_note_for_a_different_patient_blocks(self):
        v = evaluate_claim(claim(iv_note_patient_mismatch=1))
        self.assertFalse(v['ready'])
        self.assertIn('IV Note has patient mismatch', v['blockers'])

    def test_mismatch_blocks_even_for_an_office_visit(self):
        # The Progress Note exemption must never leak into the patient-identity
        # check — a wrong-patient IV Note is disqualifying regardless of CPT.
        v = evaluate_claim(claim(cpt='99213', iv_note_patient_mismatch=1))
        self.assertFalse(v['ready'])


class SubscriberIdMustBeConfirmed(unittest.TestCase):
    """An unconfirmed member number is never auto-sent to a payer."""

    def test_absent_subscriber_blocks(self):
        v = evaluate_claim(claim(subscriber_id=''))
        self.assertFalse(v['ready'])
        self.assertIn('Subscriber ID', v['blockers'])

    def test_unverified_subscriber_blocks_and_says_why(self):
        v = evaluate_claim(claim(subscriber_id='XEA914555915',
                                 subscriber_id_unverified=1))
        self.assertFalse(v['ready'])
        self.assertIn('Subscriber ID UNVERIFIED (HCFA box 1a not read)', v['blockers'])

    def test_unverified_message_distinguishes_from_missing(self):
        absent = evaluate_claim(claim(subscriber_id=''))['blockers']
        unverified = evaluate_claim(
            claim(subscriber_id='X', subscriber_id_unverified=1))['blockers']
        self.assertNotEqual(absent, unverified)


class TheProgressNoteDoesNotBlock(unittest.TestCase):
    """It used to, and that held 114 claims out of every run.

    112 of them because the note could not be pulled from ECW at all. The
    payer does not need it, so it is a bonus attachment now, never a gate.
    """

    def test_a_claim_with_no_progress_note_is_ready(self):
        v = evaluate_claim(claim(encounter_file_s3_path=''))
        self.assertTrue(v['ready'])
        self.assertEqual(v['blockers'], [])

    def test_a_progress_note_flagged_for_revision_does_not_block(self):
        self.assertTrue(evaluate_claim(claim(encounter_revision_needed=1))['ready'])

    def test_but_a_flagged_note_is_not_attached(self):
        # Not blocking is not the same as sending a document we distrust.
        self.assertFalse(
            evaluate_claim(claim(encounter_revision_needed=1))['attach_progress_note'])

    def test_a_good_note_is_attached(self):
        self.assertTrue(evaluate_claim(claim())['attach_progress_note'])

    def test_a_missing_note_is_not_attached(self):
        self.assertFalse(evaluate_claim(claim(encounter_file_s3_path=''))['attach_progress_note'])

    def test_the_other_documents_still_gate(self):
        # Dropping this requirement must not loosen the rest.
        self.assertFalse(evaluate_claim(claim(hcfa_s3_path=''))['ready'])
        self.assertFalse(evaluate_claim(claim(prog_notes_s3_path=''))['ready'])
        self.assertFalse(evaluate_claim(claim(subscriber_id=''))['ready'])
        self.assertFalse(evaluate_claim(claim(iv_note_patient_mismatch=1))['ready'])


class TheSubmissionStepHonoursIt(unittest.TestCase):
    def test_it_only_attaches_a_note_the_gate_approves(self):
        import os
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'src', 'main.py')
        with open(main, encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn("evaluate_claim(claim_item)['attach_progress_note']", src)

    def test_two_documents_are_enough(self):
        import os
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'src', 'main.py')
        with open(main, encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn('_min_files = 2', src)
        self.assertNotIn('_min_files = 2 if', src)


class ProgressNoteExemptions(unittest.TestCase):
    def test_office_visit_needs_no_progress_note(self):
        v = evaluate_claim(claim(cpt='99213', encounter_file_s3_path=''))
        self.assertTrue(v['ready'])
        self.assertEqual(v['progress_note_exempt'], 'office visit')

    def test_reviewer_override_needs_no_progress_note(self):
        v = evaluate_claim(claim(encounter_file_s3_path='',
                                 progress_note_not_required=1))
        self.assertTrue(v['ready'])
        self.assertEqual(v['progress_note_exempt'], 'reviewer override')

    def test_the_override_is_now_moot_but_still_reported(self):
        # Nothing needs waiving any more; the flag is kept because reviewers
        # set it and the reason is worth showing.
        v = evaluate_claim(claim(progress_note_not_required=1,
                                 encounter_file_s3_path=''))
        self.assertTrue(v['ready'])
        self.assertEqual(v['progress_note_exempt'], 'reviewer override')

    def test_iv_therapy_is_not_exempt(self):
        self.assertEqual(evaluate_claim(claim())['progress_note_exempt'], '')


class OfficeVisitDetection(unittest.TestCase):
    def test_new_and_established_patient_codes(self):
        for code in ('99201', '99205', '99211', '99215'):
            self.assertTrue(is_office_visit(code), code)

    def test_infusion_codes_are_not_office_visits(self):
        for code in ('96365', '96366', '96375', 'J3490'):
            self.assertFalse(is_office_visit(code), code)

    def test_finds_the_code_inside_a_mixed_list(self):
        self.assertTrue(is_office_visit('96365, 96366, 99213, J3490'))

    def test_empty_and_none_are_not_office_visits(self):
        self.assertFalse(is_office_visit(None))
        self.assertFalse(is_office_visit(''))

    def test_does_not_match_a_longer_number_containing_a_code(self):
        # A 9-digit subscriber-like value must not be read as a CPT.
        self.assertFalse(is_office_visit('123992131'))


class NoDoubleSubmission(unittest.TestCase):
    """A claim already sent is never re-sent by the batch path."""

    def test_already_submitted_is_not_ready_to_submit(self):
        self.assertFalse(ready_to_submit(claim(symplisend_submitted=1)))

    def test_already_submitted_is_reported_separately_from_readiness(self):
        # The single-claim path needs to say "complete, but already sent"
        # rather than "incomplete" — so the two facts stay distinct.
        v = evaluate_claim(claim(symplisend_submitted=1))
        self.assertTrue(v['ready'])
        self.assertTrue(v['already_submitted'])
        self.assertEqual(v['blockers'], [])

    def test_incomplete_and_already_submitted_reports_both(self):
        v = evaluate_claim(claim(symplisend_submitted=1, hcfa_s3_path=''))
        self.assertFalse(v['ready'])
        self.assertTrue(v['already_submitted'])


class BlockersAreComplete(unittest.TestCase):
    def test_a_claim_with_nothing_lists_every_reason(self):
        v = evaluate_claim({'claim_id': '1'})
        self.assertFalse(v['ready'])
        for expected in ('HCFA', 'IV Note', 'Subscriber ID'):
            self.assertIn(expected, v['blockers'])
        self.assertNotIn('Progress Note', v['blockers'])

    def test_ready_is_exactly_the_absence_of_blockers(self):
        for c in (claim(), claim(hcfa_s3_path=''), claim(subscriber_id=''),
                  claim(encounter_file_s3_path=''), {'claim_id': 'x'}):
            self.assertEqual(evaluate_claim(c)['ready'],
                             not evaluate_claim(c)['blockers'])


if __name__ == '__main__':
    unittest.main()
