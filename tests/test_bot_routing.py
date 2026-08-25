"""Each bot must consume its own queue and drive its own browser.

Two bots run side by side — submissions and resubmissions — each with its own
systemd unit, X display, noVNC port, SQS queue and Chrome profile. The routing
used to have a fallback: anything that was not 'iv_corrections' polled the
submissions queue, so the resubmissions bot silently competed with the
submissions bot for the same messages while its own queue was never read.
These tests exist so that cannot come back.
"""
import unittest

from src.aws.clients import QUEUE_BY_ROLE, ROLE_ENV_VAR
from src.config import Settings
from src.ecw.browser import profile_dir_for

ROLES = ('submissions', 'resubmissions')

QUEUES = dict(
    sqs_queue_url='https://sqs/helixona-agent-tasks',
    sqs_queue_url_resub='https://sqs/helixona-agent-tasks-resub',
)


def settings_for(role):
    return Settings(bot_role=role, **QUEUES)


class EachRoleHasItsOwnQueue(unittest.TestCase):
    def test_every_role_is_mapped(self):
        for role in ROLES:
            self.assertIn(role, QUEUE_BY_ROLE, role)

    def test_roles_resolve_to_distinct_queues(self):
        urls = [QUEUE_BY_ROLE[r](settings_for(r)) for r in ROLES]
        self.assertEqual(len(set(urls)), len(ROLES), f'queues overlap: {urls}')

    def test_resubmissions_polls_the_resubmissions_queue(self):
        url = QUEUE_BY_ROLE['resubmissions'](settings_for('resubmissions'))
        self.assertTrue(url.endswith('helixona-agent-tasks-resub'), url)

    def test_resubmissions_does_not_poll_the_submissions_queue(self):
        # The exact regression: a bot with its own display and profile eating
        # the submissions bot's messages.
        self.assertNotEqual(
            QUEUE_BY_ROLE['resubmissions'](settings_for('resubmissions')),
            QUEUE_BY_ROLE['submissions'](settings_for('submissions')))

    def test_every_role_names_the_env_var_that_configures_it(self):
        for role in ROLES:
            self.assertIn(role, ROLE_ENV_VAR, role)
        self.assertEqual(ROLE_ENV_VAR['resubmissions'], 'SQS_QUEUE_URL_RESUB')


class SettingsReadTheResubmissionQueue(unittest.TestCase):
    def test_the_field_exists(self):
        # Without this field pydantic silently drops SQS_QUEUE_URL_RESUB from
        # .env, which is how the queue came to be configured but unused.
        self.assertTrue(hasattr(Settings(), 'sqs_queue_url_resub'))

    def test_it_is_populated_from_config(self):
        self.assertEqual(settings_for('resubmissions').sqs_queue_url_resub,
                         QUEUES['sqs_queue_url_resub'])


class UnknownRolesPollNothing(unittest.TestCase):
    """Idling is safe; stealing another bot's work is not."""

    def test_an_unknown_role_has_no_queue(self):
        self.assertIsNone(QUEUE_BY_ROLE.get('typo_role'))

    def test_there_is_no_catch_all_entry(self):
        self.assertEqual(set(QUEUE_BY_ROLE), set(ROLES))


class EachRoleHasItsOwnBrowserProfile(unittest.TestCase):
    """Chrome locks its user-data-dir; two bots on one profile corrupt it."""

    def test_profiles_are_distinct(self):
        dirs = [profile_dir_for(r) for r in ROLES]
        self.assertEqual(len(set(dirs)), len(ROLES), f'profiles overlap: {dirs}')

    def test_submissions_keeps_the_original_path(self):
        # Changing it would drop the running bot's logged-in session on deploy.
        self.assertEqual(profile_dir_for('submissions'),
                         '/opt/helixona-agent/browser-profile')

    def test_resubmissions_uses_the_profile_that_exists_on_the_host(self):
        self.assertEqual(profile_dir_for('resubmissions'),
                         '/opt/helixona-agent/browser-profile-resubmissions')

    def test_an_unknown_role_gets_its_own_directory(self):
        d = profile_dir_for('something_new')
        self.assertNotIn(d, [profile_dir_for(r) for r in ROLES])


class DashboardAgreesWithTheAgent(unittest.TestCase):
    """Two copies of one mapping drift; this notices when they do."""

    def test_the_dashboard_routes_the_same_roles(self):
        import os
        os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
        os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
        os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
        import dashboard
        self.assertEqual(set(dashboard.BOT_ROUTING), set(QUEUE_BY_ROLE))

    def test_each_bot_has_a_distinct_service_and_novnc_port(self):
        import dashboard
        services = [b['service'] for b in dashboard.BOT_ROUTING.values()]
        ports = [b['novnc_port'] for b in dashboard.BOT_ROUTING.values()]
        self.assertEqual(len(set(services)), len(services), services)
        self.assertEqual(len(set(ports)), len(ports), ports)

    def test_every_tab_has_at_least_one_task_option(self):
        """A tab whose options are all hidden leaves an unopenable dropdown.

        setActiveBot hides every task option not owned by the active bot. When
        the resubmissions tab was added without tagging any option for it, all
        of them were hidden at once and the Send Task dropdown could not be
        opened from that tab.
        """
        import re
        import dashboard
        html = dashboard.DASHBOARD_HTML
        owners = re.findall(r'<option [^>]*data-bot="([^"]+)"', html)
        for bot in dashboard.BOT_ROUTING:
            available = [o for o in owners if bot in o.split()]
            self.assertTrue(available, f'no task option is available for tab {bot!r}')

    def test_blue_shield_tasks_are_offered_to_both_blue_shield_bots(self):
        # Same dispatcher, same task types; only the queue differs.
        import re
        import dashboard
        for value in ('bs_missing_docs', 'blueshield_submissions', 'ecw_status_update'):
            m = re.search(r'<option value="%s" data-bot="([^"]+)"' % value,
                          dashboard.DASHBOARD_HTML)
            self.assertIsNotNone(m, value)
            self.assertEqual(set(m.group(1).split()), {'submissions', 'resubmissions'}, value)

    def test_resubmissions_points_at_its_own_unit(self):
        import dashboard
        self.assertEqual(dashboard.BOT_ROUTING['resubmissions']['service'],
                         'helixona-agent-resub')
        self.assertEqual(dashboard.BOT_ROUTING['resubmissions']['novnc_port'], 6081)


class EcwStatusCanBeRechecked(unittest.TestCase):
    """Claims we believe are updated can drift back.

    Claim 13 was recorded as set to "Claim sent via Symplisend" on 2026-06-28
    and verified at the time, yet ECW showed it in "Ready to Bill - Symplisend
    CC" weeks later. The resubmissions bot extracts on exactly that status, so
    every stale one is re-walked on every run.
    """

    def _main(self):
        import os
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'src', 'main.py')
        with open(p, encoding='utf-8') as fh:
            return fh.read()

    def test_recheck_revisits_claims_already_marked_updated(self):
        src = self._main()
        self.assertIn("recheck = bool(body.get('recheck'))", src)
        self.assertIn("and (recheck or not item.get('ecw_status_updated'))", src)

    def test_without_recheck_only_the_never_updated_are_touched(self):
        # The default must stay cheap and narrow.
        src = self._main()
        self.assertIn("not item.get('ecw_status_updated')", src)

    def test_a_claim_already_correct_is_left_alone(self):
        # What makes a recheck over hundreds of claims affordable: read first,
        # write only where ECW actually disagrees.
        src = self._main()
        self.assertIn('already reads', src)
        i = src.index('Claim Status BEFORE')
        j = src.index('STEP 3 — open the Claim Status Code picker')
        self.assertIn('return True', src[i:j])


if __name__ == '__main__':
    unittest.main()
