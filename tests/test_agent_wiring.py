"""Structural guards on the agent itself.

The submission flow is thousands of lines of browser automation that cannot be
exercised without a payer portal. What *can* be checked cheaply, on every
deploy, is that it still imports, that it still dispatches the task types the
queues send it, and that no exit path lost its audit call in a refactor — the
failure mode that would quietly return us to having no evidence.
"""
import ast
import os
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, 'src', 'main.py')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class TheAgentImports(unittest.TestCase):
    """A syntax or import error here means a restart into a dead bot."""

    def test_main_imports_cleanly(self):
        import src.main  # noqa: F401

    def test_audit_helpers_are_wired_in(self):
        import src.main as main
        for name in ('record_submission', 'describe_documents',
                     'OUTCOME_SUBMITTED', 'OUTCOME_FAILED', 'OUTCOME_BLOCKED'):
            self.assertTrue(hasattr(main, name), name)

    def test_submission_gate_is_wired_in(self):
        import src.main as main
        self.assertTrue(callable(main.ready_to_submit))
        self.assertTrue(callable(main.evaluate_claim))
        self.assertTrue(callable(main.is_office_visit))


class TaskTypesStillDispatch(unittest.TestCase):
    """The queues send these strings; losing one strands work silently."""

    EXPECTED = [
        'blueshield_submissions',
        'ecw_status_update',
        'generate_hcfa',
        'capture_blueshield_claim',
        'verify_medical_record',
        'generate_cover_letter',
        'process_adjudication',
        'nightly_bulk_extract',
        'fix_coding_ivs',
        'bs_missing_docs',
        'check_reconcile',
    ]

    def test_every_known_task_type_is_handled(self):
        source = _read(MAIN)
        for task in self.EXPECTED:
            self.assertIn(f"task_type == '{task}'", source, task)


class EverySubmissionExitIsAudited(unittest.TestCase):
    """No path out of a submission attempt may leave the log silent.

    The point of the audit log is that a failed or blocked attempt is recorded
    just as firmly as a successful one. Counting the call sites is crude, but
    it fails loudly if a refactor drops one — which is exactly the regression
    that would put us back where we started.
    """

    # 7: success, the two post-submit failures, the New Submission open
    # failure, the insufficient-documents block, the unhandled-error handler,
    # and the prior-claim-number block added with the resubmission form.
    EXPECTED_CALL_SITES = 7

    def _submission_calls(self):
        tree = ast.parse(_read(MAIN))
        return [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'record_submission'
        ]

    def test_all_exit_paths_still_record(self):
        found = len(self._submission_calls())
        self.assertEqual(
            found, self.EXPECTED_CALL_SITES,
            f'expected {self.EXPECTED_CALL_SITES} record_submission call sites, '
            f'found {found}. If you deliberately added or removed a submission '
            f'exit path, update EXPECTED_CALL_SITES — but first make sure the '
            f'new path still writes an audit row.'
        )

    def test_every_call_names_its_outcome(self):
        for call in self._submission_calls():
            kwargs = {kw.arg for kw in call.keywords}
            self.assertIn('outcome', kwargs)
            self.assertIn('documents', kwargs)

    def test_outcomes_used_are_the_defined_constants(self):
        allowed = {'OUTCOME_SUBMITTED', 'OUTCOME_FAILED', 'OUTCOME_BLOCKED'}
        for call in self._submission_calls():
            for kw in call.keywords:
                if kw.arg == 'outcome':
                    self.assertIsInstance(kw.value, ast.Name)
                    self.assertIn(kw.value.id, allowed)


class SubmissionRuleIsStatedOnce(unittest.TestCase):
    """It used to be written twice, and the two copies drifted."""

    def test_main_does_not_reimplement_the_gate(self):
        source = _read(MAIN)
        # Signatures of the old inline copies. The field names themselves still
        # appear legitimately elsewhere — they are *written* during HCFA
        # capture — so match on the decision logic, not the vocabulary.
        self.assertNotIn("missing.append('HCFA')", source)
        self.assertNotIn('has_hcfa and has_prog_notes and has_subscriber', source)


if __name__ == '__main__':
    unittest.main()


class NamedClaimsRunAgain(unittest.TestCase):
    """2026-09-19: 24 IV claims read 'Documentation Completed' with nothing
    behind them. A list of claims can be run again, everything collected."""

    def test_the_list_comes_from_claim_ids_or_the_test_box(self):
        from src.main import _claim_id_list
        self.assertEqual(_claim_id_list({'claim_ids': [6234, '3865', 6234]}), ['6234', '3865'])
        self.assertEqual(_claim_id_list({'claim_ids': '6234, 3865;6455 3897'}), ['6234', '3865', '6455', '3897'])
        self.assertEqual(_claim_id_list({'test_claim_id': '239, 240'}), ['239', '240'])
        self.assertEqual(_claim_id_list({'claim_ids': ['1'], 'test_claim_id': '2'}), ['1', '2'])
        self.assertEqual(_claim_id_list({}), [])

    def test_redo_collects_everything_again(self):
        from src.main import _claim_needs
        stored = {'hcfa_s3_path': 's3://b/h.pdf', 'prog_notes_s3_path': 's3://b/p.pdf', 'subscriber_id': 'X1',
                  'encounter_date': '04/10/2026', 'encounter_file_s3_path': 's3://b/e.pdf', 'iv_note_rx_start_date': '04/10/2026',
                  'patient_name': 'Pat', 'dos': '04/10/2026'}
        kept = _claim_needs('6234', None, stored)
        self.assertFalse(any(v for k, v in kept.items() if k.startswith('needs_')))
        self.assertEqual((kept['patient_name'], kept['service_date']), ('Pat', '04/10/2026'))
        again = _claim_needs('6234', {'patient': 'Pat P.', 'serviceDate': '04/11/2026', 'pageNum': 3}, stored, redo=True)
        self.assertTrue(all(v for k, v in again.items() if k.startswith('needs_')))
        self.assertEqual((again['patient_name'], again['service_date'], again['page_num']), ('Pat P.', '04/11/2026', 3))
        # Nothing stored, nothing on the page: every step is needed anyway.
        self.assertTrue(_claim_needs('3865', None, {})['needs_hcfa'])

    def test_the_pipeline_forwards_the_list_and_the_page_says_when_nothing_is_behind_a_stage(self):
        src = open('src/main.py', encoding='utf-8').read()
        self.assertIn("step_body['claim_ids'] = body['claim_ids']", src)
        self.assertIn("redo = bool(body.get('redo', bool(body.get('claim_ids'))))", src)
        self.assertIn("claims_to_process = [by_cid[t] for t in target_ids if t in by_cid]", src)
        self.assertIn("if testing_mode and not target_ids and hcfa_success_count + hcfa_fail_count >= 1:", src)
        d = open('dashboard.py', encoding='utf-8').read()
        self.assertIn("if (key === 'documentation' && c && !c.hcfa_s3_path && !c.prog_notes_s3_path) {", d)
        self.assertIn('Documentation Pending', d)
        self.assertIn("${missing.join(' + ')} missing", d)
        self.assertIn("${getStagePill(state, c)}", d)
        self.assertIn('placeholder="e.g. 239 or 239, 240, 241"', d)


class TheHcfaFillsWhatTheClaimsPageDidNotSay(unittest.TestCase):
    """2026-09-19: claims run again by number were not on the Claims page,
    so nothing knew the patient — and the captures dropped every file."""

    def _words(self):
        w = lambda text, x, y: {'text': text, 'x': x, 'y': y}
        return [[
            w("PATIENT'S", 0.02, 0.112), w('NAME', 0.08, 0.112), w('(Last', 0.11, 0.112), w('Name,', 0.15, 0.112),
            w('GRAY,', 0.03, 0.128), w('CASSANDRA', 0.09, 0.128),
            w('SMITH,', 0.60, 0.128), w('JOHN', 0.66, 0.128),                  # box 4, the insured — not ours
            w('04', 0.02, 0.70), w('10', 0.06, 0.70), w('26', 0.10, 0.70), w('11', 0.24, 0.70), w('96365', 0.31, 0.70), w('325', 0.63, 0.70), w('00', 0.68, 0.70), w('1', 0.71, 0.70),
            w('04', 0.02, 0.72), w('10', 0.06, 0.72), w('26', 0.10, 0.72), w('11', 0.24, 0.72), w('J3490', 0.31, 0.72), w('2550', 0.63, 0.72), w('1', 0.71, 0.72),
            w('350', 0.63, 0.89), w('50', 0.68, 0.89),                          # box 28 total charge
        ]]

    def test_patient_dos_and_charges_come_off_the_form(self):
        from src.main import _facts_from_hcfa_words
        got = _facts_from_hcfa_words(self._words())
        self.assertEqual(got, {'patient_name': 'Gray, Cassandra', 'service_date': '04/10/2026', 'charges': '350.50'})

    def test_nothing_is_guessed_when_the_layout_does_not_match(self):
        from src.main import _facts_from_hcfa_words
        self.assertEqual(_facts_from_hcfa_words([]), {'patient_name': '', 'service_date': '', 'charges': ''})
        junk = [[{'text': 'HELIXONA', 'x': 0.05, 'y': 0.12}, {'text': 'INC', 'x': 0.12, 'y': 0.12}]]
        self.assertEqual(_facts_from_hcfa_words(junk)['patient_name'], '')

    def test_the_run_fills_the_record_and_dynamodb_but_never_overwrites(self):
        from src import main as m
        saved = {}
        class _Aws:
            def update_claim_status(self, cid, data): saved[cid] = data
        rec = {'claim_id': '6455', 'patient_name': '', 'service_date': '', 'charges': ''}
        with mock.patch.object(m, '_claim_facts_from_hcfa_pdf', return_value={'patient_name': 'Gray, Cassandra', 'service_date': '04/10/2026', 'charges': '350.50'}):
            filled = m._fill_claim_facts(_Aws(), rec, '/tmp/x.pdf')
        self.assertEqual(filled, {'patient_name': 'Gray, Cassandra', 'service_date': '04/10/2026', 'charges': '350.50'})
        self.assertEqual((rec['patient_name'], rec['dos'], saved['6455']['dos']), ('Gray, Cassandra', '04/10/2026', '04/10/2026'))
        rec2 = {'claim_id': '1', 'patient_name': 'Ybarra, Jennifer', 'service_date': '09/17/2026', 'charges': '1.00'}
        with mock.patch.object(m, '_claim_facts_from_hcfa_pdf', side_effect=AssertionError('not even read')):
            self.assertEqual(m._fill_claim_facts(_Aws(), rec2, '/tmp/x.pdf'), {})
        src = open('src/main.py', encoding='utf-8').read()
        self.assertIn("_fill_claim_facts(aws_client, claim_record, _lp_f, where=' on file')", src)
        self.assertIn("_fill_claim_facts(aws_client, claim_record, download_path, where=' just generated')", src)
