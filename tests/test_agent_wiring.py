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
