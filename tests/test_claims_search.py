"""The claims table's search box.

Client-side over the cached load — clearing the box restores every row
instantly, so the audit page's slow-response race (an empty search showing one
stale result) cannot happen here.
"""
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    with open(os.path.join(REPO, 'dashboard.py'), encoding='utf-8') as fh:
        return fh.read()


class TheSearchBoxExists(unittest.TestCase):
    def test_it_sits_in_the_claims_section_title(self):
        src = _src()
        i = src.index('id="claims-section-submissions"')
        head = src[i:src.index('claims-table-wrap', i)]
        self.assertIn('id="claims-search"', head)

    def test_it_names_the_fields_it_searches(self):
        src = _src()
        i = src.index('id="claims-search"')
        self.assertIn('BS ref', src[i:i + 300])


class EveryRenderGoesThroughTheSearch(unittest.TestCase):
    def _render(self):
        src = _src()
        i = src.index('function renderClaims(claims)')
        return src[i:i + 1600]

    def test_the_filter_is_applied_inside_render_claims(self):
        # Inside the renderer, so every caller — poll, tab switch, date
        # filter — gets it without knowing it exists.
        self.assertIn('claims = applyClaimsSearch(claims);', self._render())

    def test_an_empty_query_changes_nothing(self):
        src = _src()
        i = src.index('function applyClaimsSearch')
        self.assertIn('if (!q) return claims;', src[i:i + 600])

    def test_every_term_must_match(self):
        # "fletcher 08/07" narrows; it does not union.
        src = _src()
        i = src.index('function applyClaimsSearch')
        self.assertIn('terms.every(t => hay.includes(t))', src[i:i + 900])

    def test_the_bs_ref_is_searchable(self):
        src = _src()
        i = src.index('function applyClaimsSearch')
        self.assertIn('c.original_ref_no', src[i:i + 900])

    def test_a_no_match_says_so_instead_of_no_claims_yet(self):
        self.assertIn('No claims match the search.', self._render())

    def test_the_meta_line_reports_the_match_count(self):
        self.assertIn('claims match', self._render())


class TypingDoesNotRefetch(unittest.TestCase):
    def test_input_rerenders_from_the_cached_load(self):
        src = _src()
        i = src.index('function onClaimsSearch()')
        block = src[i:i + 700]
        self.assertIn('window._allClaims', block)
        self.assertNotIn("fetch('/api/claims')", block)

    def test_keystrokes_are_debounced(self):
        src = _src()
        i = src.index('function onClaimsSearch()')
        self.assertIn('setTimeout', src[i:i + 500])
        self.assertIn('clearTimeout', src[i:i + 500])


if __name__ == '__main__':
    unittest.main()
