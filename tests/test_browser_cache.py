"""Run actual-source browser regressions with Node's dependency-free test runner.

Native-browser (real decoder + real IndexedDB) coverage lives in
tests/browser-native and runs in its own CI gate, where Playwright and
Chromium are guaranteed; it must never skip here.
"""
from pathlib import Path
import shutil
import subprocess
import unittest
import os
import sys


class TestBrowserCache(unittest.TestCase):
    def test_browser_cache_regressions(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required to exercise the production browser source")
        result = subprocess.run([node, "--test", str(Path(__file__).with_suffix(".js"))],
                                text=True, capture_output=True, timeout=60,
                                env={**os.environ, "PYTHON": sys.executable})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
