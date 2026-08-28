"""The hero card's date filter.

The headline said "907 of 907 claims submitted" with no way to ask "and how
many of those were for July?" or "what went out this week?". The filter narrows
everything the page shows — the counter, the pipeline, the stats and the
table — by service date or by the date the packet was sent.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    with open(os.path.join(REPO, 'dashboard.py'), encoding='utf-8') as fh:
        return fh.read()


def _js():
    src = _src()
    i = src.index('// ---- Date filter (hero card) ----')
    return src[i:src.index('function renderStats', i)]


class TheFilterExists(unittest.TestCase):
    def test_the_controls_are_in_the_hero_card(self):
        src = _src()
        i = src.index('class="hero-kpi" id="hero-submissions"')
        hero = src[i:src.index('<!-- MAIN', i)]
        for ctl in ('id="df-from"', 'id="df-to"', 'id="df-field"', 'df-clear'):
            self.assertIn(ctl, hero, ctl)

    def test_both_date_semantics_are_offered(self):
        src = _src()
        self.assertIn('value="dos"', src)
        self.assertIn('value="sent"', src)

    def test_the_date_inputs_render_dark(self):
        # A native date input renders a white calendar unless told otherwise.
        self.assertIn('color-scheme:dark', _src())


class BothDateShapesParse(unittest.TestCase):
    """dos is "MM/DD/YYYY"; symplisend_submitted_at is "YYYY-MM-DD HH:MM:SS UTC".

    Both normalise to ISO so <input type=date> values compare as strings.
    """

    def test_dos_is_reordered_to_iso(self):
        self.assertIn('`${m[3]}-${m[1]}-${m[2]}`', _js())

    def test_sent_takes_the_leading_iso_date(self):
        self.assertIn('symplisend_submitted_at', _js())

    def test_a_claim_without_the_date_is_excluded_when_filtering(self):
        # A dateless claim cannot be proven inside the range.
        self.assertIn("if (!d) return false;", _js())

    def test_no_filter_means_no_change(self):
        self.assertIn('if (!from && !to) return claims;', _js())


class TheWholePageFollowsTheFilter(unittest.TestCase):
    def test_the_table_load_goes_through_the_filter(self):
        self.assertIn('applyDateFilter(claimsForActiveBot(data.claims))', _src())

    def test_a_filter_change_rerenders_from_cache(self):
        # Not a 3MB refetch per keystroke.
        js = _js()
        self.assertIn('window._allClaims', js)
        self.assertIn('renderClaims(claims)', js)

    def test_the_counter_uses_the_servers_own_definition_of_submitted(self):
        # One definition, two callers — the filtered headline must not drift
        # from /api/claim-counts.
        src = _src()
        self.assertIn('PIPELINE_STAGES.submitted.states', src)
        i = src.index('async function loadCounts()')
        self.assertIn('dateFilterActive()', src[i:i + 600])

    def test_the_unfiltered_path_still_uses_the_fast_endpoint(self):
        src = _src()
        i = src.index('async function loadCounts()')
        self.assertIn("fetch('/api/claim-counts')", src[i:i + 2000])

    def test_the_filtered_headline_says_it_is_filtered(self):
        self.assertIn("'filtered'", _src())

    def test_clear_resets_both_dates(self):
        js = _js()
        self.assertIn("['df-from', 'df-to']", js)
        self.assertIn("el.value = ''", js)


if __name__ == '__main__':
    unittest.main()
