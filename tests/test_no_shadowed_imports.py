"""2026-09-23: BlueShield Submissions stopped at the SympliSend dashboard with
"Never reached the SympliSend dashboard — stopped at …/#/dashboard".

process_message re-imported `urlparse` in one branch. Python then treats the
name as a local of the whole function, so the hostname check (a closure in
another branch) raised NameError, which its try/except turned into "not on
SympliSend". A module-level import re-imported inside a function whose
closures use it is a trap; none may exist in src/main.py."""
import os
import ast
import symtable
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _shadowed(path):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    module_imports = {a.asname or a.name.split('.')[0] for n in tree.body
                      if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}

    def frees(t):
        out = set()
        for ch in t.get_children():
            out |= {s.get_name() for s in ch.get_symbols() if s.is_free()}
            out |= frees(ch)
        return out

    hits = []

    def walk(t):
        for c in t.get_children():
            if c.get_type() == 'function':
                local = {s.get_name() for s in c.get_symbols() if s.is_imported() and s.is_local()}
                for name in sorted(local & module_imports & frees(c)):
                    hits.append(f'{c.get_name()} (line {c.get_lineno()}): {name}')
            walk(c)
    walk(symtable.symtable(src, path, 'exec'))
    return hits


class NoClosureSeesAnUnboundImport(unittest.TestCase):
    def test_main(self):
        self.assertEqual(_shadowed(os.path.join(REPO, 'src', 'main.py')), [])

    def test_the_symplisend_check_uses_the_module_import(self):
        src = open(os.path.join(REPO, 'src', 'main.py'), encoding='utf-8').read()
        self.assertIn("return 'symplisend' in urlparse(url).netloc.lower()", src)
        self.assertEqual(src.count('from urllib.parse import urlparse'), 1)


if __name__ == '__main__':
    unittest.main()
