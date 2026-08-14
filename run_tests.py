#!/usr/bin/env python3
"""Run the test suite. Exits non-zero on any failure, so a deploy can gate on it.

    python3 run_tests.py            # all tests
    python3 run_tests.py -v         # per-test names

Stdlib only — no pytest to install on the agent host.
"""
import logging
import os
import sys
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# The agent logs each audit row at INFO; useful in production, noise in a test
# run where the assertions are the output that matters.
logging.disable(logging.CRITICAL)

# Placeholders so importing the dashboard does not require real credentials.
# Tests inject fakes; nothing here ever reaches AWS.
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('AWS_REGION', 'us-west-2')
os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')


def main():
    verbosity = 2 if '-v' in sys.argv else 1
    suite = unittest.TestLoader().discover(
        start_dir=os.path.join(REPO, 'tests'), top_level_dir=REPO)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)

    print()
    if result.wasSuccessful():
        print(f'PASS — {result.testsRun} tests')
        return 0
    print(f'FAIL — {len(result.failures)} failed, {len(result.errors)} errored, '
          f'of {result.testsRun}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
