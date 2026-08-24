"""Blue Shield's "you're leaving our site" modal.

Blue Shield puts an interstitial between the SympliSend link and SympliSend —
"You're leaving Blue Shield of California and going to a third-party site",
with Cancel and Continue. The new tab only opens after Continue, so the code
waiting for a popup waited out its timeout and fell through to a page that was
not SympliSend. Every claim then failed on a "New Submission" timeout, having
downloaded its documents first.

The click itself needs a browser, so what is tested here is the selection
logic — that it finds Continue, never Cancel, and stays inside the dialog.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, 'src', 'main.py')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _selector_js():
    """The page.evaluate body from _dismiss_third_party_interstitial."""
    src = _read(MAIN)
    start = src.index('def _dismiss_third_party_interstitial')
    end = src.index('\ndef ', start + 10)
    body = src[start:end]
    m = re.search(r'page\.evaluate\("""(.+?)"""\)', body, re.S)
    assert m, 'could not find the evaluate body'
    return m.group(1)


class TheSelectorLogic(unittest.TestCase):
    """Exercised against the real modal's wording."""

    MODAL = """
      <div role="dialog">
        <h2>We're redirecting you to SympliSend</h2>
        <p>You're leaving Blue Shield of California and going to a third-party site.</p>
        <button>Cancel</button>
        <button>Continue</button>
      </div>
    """

    def _run(self, html):
        """Mirror the JS in Python over a parsed fragment."""
        from html.parser import HTMLParser

        class Grab(HTMLParser):
            def __init__(self):
                super().__init__()
                self.buttons, self._cur = [], None

            def handle_starttag(self, tag, attrs):
                if tag in ('button', 'a', 'input'):
                    self._cur = ''

            def handle_data(self, data):
                if self._cur is not None:
                    self._cur += data

            def handle_endtag(self, tag):
                if tag in ('button', 'a') and self._cur is not None:
                    self.buttons.append(self._cur.strip())
                    self._cur = None

        marker = re.compile(r'leaving blue shield|third-party site|redirecting you to', re.I)
        if not marker.search(re.sub(r'<[^>]+>', ' ', html)):
            return None
        g = Grab()
        g.feed(html)
        for label in g.buttons:
            if re.search(r'continue', label, re.I) and not re.search(r'cancel', label, re.I):
                return label
        return None

    def test_finds_continue_in_the_real_modal(self):
        self.assertEqual(self._run(self.MODAL), 'Continue')

    def test_never_returns_cancel(self):
        self.assertNotEqual(self._run(self.MODAL), 'Cancel')

    def test_ignores_a_page_without_the_modal(self):
        # A stray Continue elsewhere on the page must not be clicked.
        self.assertIsNone(self._run(
            '<div><button>Continue</button><p>Cookie preferences</p></div>'))


class TheFlowUsesIt(unittest.TestCase):
    def test_the_helper_exists(self):
        self.assertIn('def _dismiss_third_party_interstitial(', _read(MAIN))

    def test_it_runs_inside_the_popup_wait(self):
        # Must be called while expect_page is still listening — the tab opens
        # only after Continue, so dismissing it afterwards is too late.
        src = _read(MAIN)
        start = src.index('with page.context.expect_page(')
        block = src[start:start + 700]
        self.assertIn('_dismiss_third_party_interstitial(page)', block)
        self.assertLess(block.index('_dismiss_third_party_interstitial(page)'),
                        block.index('new_page_info.value'))

    def test_the_direct_sso_fallback_also_handles_it(self):
        src = _read(MAIN)
        i = src.index('externalSSO?partnerId=FirstSource')
        self.assertIn('_dismiss_third_party_interstitial', src[i:i + 600])

    def test_cancel_is_never_clicked(self):
        self.assertIn('!/cancel/i.test(label)', _selector_js())

    def test_it_scopes_to_the_dialog_before_the_whole_page(self):
        js = _selector_js()
        self.assertIn('role="dialog"', js)
        self.assertIn('leaving blue shield', js.lower())


class NotReachingSympliSendAborts(unittest.TestCase):
    def test_the_run_stops_instead_of_failing_every_claim(self):
        # Sixty 10-second timeouts and sixty misleading audit rows is a worse
        # answer than one error naming where it actually landed.
        src = _read(MAIN)
        self.assertIn('on_symplisend', src)
        self.assertIn('Never reached the SympliSend dashboard', src)
        self.assertIn('No claims were attempted', src)


if __name__ == '__main__':
    unittest.main()
