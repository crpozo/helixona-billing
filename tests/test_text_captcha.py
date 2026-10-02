"""eCW's text captcha ("Enter Captcha text here") on the login and re-auth
pages: read off the page, solved through 2Captcha, retried on a wrong answer."""
import os
import unittest
from unittest import mock

from src.ecw import text_captcha as tc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class FakeEl:
    def __init__(self, visible=True):
        self.visible = visible
        self.typed = ''
        self.clicks = 0

    def is_visible(self):
        return self.visible

    def screenshot(self, type='png'):
        return b'\x89PNG fake'

    def fill(self, v):
        self.typed = v

    def type(self, v, delay=0):
        self.typed += v

    def click(self):
        self.clicks += 1


class FakeFrame:
    def __init__(self, inp=None, img=None, refresh=None):
        self.inp, self.img, self.refresh = inp, img, refresh
        self.url = 'https://x.ecwcloud.com/mobiledoc/jsp/webemr/login/newLogin.jsp'

    def query_selector(self, sel):
        if sel.startswith('img['):
            return self.img
        if 'placeholder*="aptcha"' in sel:
            return self.inp
        if 'Try another' in sel:
            return self.refresh
        if '#Login' in sel:
            return FakeEl()
        return None


class FakeReq:
    def __init__(self, answers):
        self.answers, self.posts, self.gets = list(answers), [], []

    def post(self, url, data=None, timeout=0):
        self.posts.append(data)
        return mock.Mock(json=lambda: {'status': 1, 'request': 'task-%d' % len(self.posts)})

    def get(self, url, params=None, timeout=0):
        self.gets.append(params)
        if params.get('action') == 'reportbad':
            return mock.Mock(json=lambda: {})
        return mock.Mock(json=lambda: {'status': 1, 'request': self.answers.pop(0)})


class SolveOne(unittest.TestCase):
    def test_no_captcha_on_the_page_is_not_an_error(self):
        self.assertEqual(tc.solve_text_captcha(FakeFrame(), mock.Mock(), FakeReq([]), 'login page'), ('none', ''))

    def test_image_goes_to_2captcha_as_base64_and_the_text_is_typed(self):
        inp, img = FakeEl(), FakeEl()
        aws = mock.Mock(); aws.get_secret.return_value = {'api_key': 'k'}
        req = FakeReq(['pe374'])
        with mock.patch.object(tc.time, 'sleep'):
            state, task = tc.solve_text_captcha(FakeFrame(inp, img), aws, req, 'login page')
        self.assertEqual((state, task), ('filled', 'task-1'))
        self.assertEqual(inp.typed, 'pe374')
        self.assertEqual(req.posts[0]['method'], 'base64')
        self.assertTrue(req.posts[0]['body'])

    def test_wrong_answer_is_reported_and_the_image_refreshed(self):
        inp, img, refresh = FakeEl(), FakeEl(), FakeEl()
        frame = FakeFrame(inp, img, refresh)
        page = mock.Mock(); page.url = frame.url; page.frames = [frame]
        aws = mock.Mock(); aws.get_secret.return_value = {'api_key': 'k'}
        req = FakeReq(['wrong1', 'right2', 'x'])
        urls = iter([frame.url, 'https://x.ecwcloud.com/mobiledoc/jsp/webemr/index.jsp'])
        type(page).url = mock.PropertyMock(side_effect=lambda: next(urls))
        with mock.patch.object(tc.time, 'sleep'):
            ok = tc.submit_with_captcha(frame, page, aws, req, '#Login', where='re-auth page')
        self.assertTrue(ok)
        self.assertEqual(refresh.clicks, 1)
        self.assertTrue(any(g.get('action') == 'reportbad' and g.get('id') == 'task-1' for g in req.gets))


class TheLoginFlowsUseIt(unittest.TestCase):
    def test_every_reauth_block_submits_through_the_captcha_helper(self):
        m = _read('src/main.py')
        self.assertEqual(m.count("submit_with_captcha(\n"), 3)
        self.assertIn("solve_text_captcha(login_frame, aws_client, _http, where='login page')", m)
        self.assertNotIn('submit_btn = reauth_frame.query_selector(', m)


if __name__ == '__main__':
    unittest.main()
