"""Which ECW claim status each bot extracts from.

ECW carries the new-submission / resubmission split in the claim status, and
the two bots divide along it. The extractor hardcoded the first-time status for
every bot, so claims sitting in "Ready to Bill - Symplisend CC" could never be
picked up — a run would finish reporting success having seen only half the
board.
"""
import ast
import os
import unittest

from src.ecw.claim_status import (
    DEFAULT_ECW_STATUS,
    ECW_STATUS_BY_ROLE,
    ecw_status_for,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, 'src', 'main.py')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class EachBotExtractsItsOwnStatus(unittest.TestCase):
    def test_submissions_takes_first_time_claims(self):
        self.assertEqual(ecw_status_for('submissions'),
                         'Ready to Submit to Symplisend')

    def test_resubmissions_takes_ready_to_bill(self):
        # The 785 claims the extractor could not see.
        self.assertEqual(ecw_status_for('resubmissions'),
                         'Ready to Bill - Symplisend CC')

    def test_the_two_bots_do_not_extract_the_same_status(self):
        self.assertNotEqual(ecw_status_for('submissions'),
                            ecw_status_for('resubmissions'))

    def test_an_unknown_role_falls_back_to_first_time(self):
        # Conservative: no new class of claim enters the pipeline by surprise.
        self.assertEqual(ecw_status_for('typo_role'), DEFAULT_ECW_STATUS)
        self.assertEqual(DEFAULT_ECW_STATUS, 'Ready to Submit to Symplisend')

    def test_statuses_are_exact_ecw_labels(self):
        # Matched against the dropdown's option text, so a stray space or a
        # different dash silently selects nothing.
        for status in ECW_STATUS_BY_ROLE.values():
            self.assertEqual(status, status.strip())
            self.assertIn('Symplisend', status)


class TheExtractorUsesTheMapping(unittest.TestCase):
    def _generate_hcfa_block(self):
        """The extractor that bs_missing_docs runs, sliced from its dispatcher."""
        src = _read(MAIN)
        start = src.index("elif task_type == 'generate_hcfa'")
        nxt = src.find("\n    elif task_type ==", start + 10)
        block = src[start:nxt if nxt != -1 else len(src)]
        # Drop comment lines — prose may name a status while the code does not.
        return '\n'.join(l for l in block.split('\n') if not l.strip().startswith('#'))

    def test_the_extractor_asks_the_mapping_which_status_to_use(self):
        self.assertIn('ecw_status_for(_settings.bot_role)', self._generate_hcfa_block())

    def test_the_extractor_no_longer_hardcodes_a_status(self):
        # The literal still lives in the fix_coding_ivs branch, which is the IV
        # bot's own flow — this asserts only about the Blue Shield extractor.
        block = self._generate_hcfa_block()
        self.assertNotIn("'Ready to Submit to Symplisend'", block)
        self.assertNotIn('"Ready to Submit to Symplisend"', block)

    def test_a_failed_filter_aborts_instead_of_extracting_defaults(self):
        # With no filter applied ECW returns its own default set, which is not
        # this bot's work; processing it would pull the wrong claims.
        src = _read(MAIN)
        self.assertIn('status_filter_set', src)
        self.assertIn('aborting so we do not pull the wrong claims', src)


class RoleGuardsCoverEveryBot(unittest.TestCase):
    def test_only_the_iv_bot_accepts_iv_tasks(self):
        # The guard used to name 'submissions' explicitly, so the resubmissions
        # bot — added later — would have accepted IV fix-coding work.
        src = _read(MAIN)
        self.assertIn("_settings.bot_role != 'iv_corrections' and task_type in IV_CORRECTIONS_TASKS",
                      src)
        self.assertNotIn("_settings.bot_role == 'submissions' and task_type in IV_CORRECTIONS_TASKS",
                         src)


if __name__ == '__main__':
    unittest.main()
