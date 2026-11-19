"""Static invariants for credential-free PR gates; no GitHub API calls."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[3]


class WorkflowTests(unittest.TestCase):
    def test_offline_workflow_has_no_privileged_execution_or_deploy(self):
        workflow = (ROOT / ".github/workflows/offline-safety.yml").read_text()
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn("  pull_request:", workflow)
        self.assertIn("branches: [main]", workflow)
        self.assertIn("  workflow_dispatch:", workflow)
        self.assertNotRegex(workflow, r"\$\{\{\s*secrets[.\[]")
        for forbidden in ("pull_request_target:", "id-token:", "sam deploy", "sam sync", "--apply", "self-hosted"):
            self.assertNotIn(forbidden, workflow)
        uses = re.findall(r"uses:\s*([^\s]+)", workflow)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[a-zA-Z0-9_./-]+@[0-9a-f]{40}$")
        self.assertEqual(len(re.findall(r"runs-on: ubuntu-24.04", workflow)), 5)
        self.assertEqual(len(re.findall(r"timeout-minutes: (10|15)\n", workflow)), 5)
        self.assertIn("node-version: '24.11.1'", workflow)
        self.assertIn("python-version: '3.12'", workflow)

    def test_native_browser_gate_is_pinned_loopback_and_fail_closed(self):
        workflow = (ROOT / ".github/workflows/offline-safety.yml").read_text()
        native = workflow.split("\n  browser-native:\n", 1)[1].split("\n  infrastructure:\n", 1)[0]
        for required in (
            "runs-on: ubuntu-24.04", "timeout-minutes: 15",
            "python-version: '3.12'", "persist-credentials: false",
            "python -m pip install playwright==1.53.0",
            "python -m playwright install --with-deps chromium",
            "test -s tests/browser-native/run.py", "test -s tests/browser-native/bridge.js",
            'python tests/browser-native/run.py --artifacts "$RUNNER_TEMP/frame-browser-native"',
            's["run"] > 0', 's["skipped"] == s["failures"] == s["errors"] == 0',
            'frame-browser-native/evidence.json',
            "AWS_ACCESS_KEY_ID: ''", "AWS_SECRET_ACCESS_KEY: ''",
            "AWS_SESSION_TOKEN: ''", "AWS_PROFILE: ''",
            "AWS_SHARED_CREDENTIALS_FILE: /dev/null", "AWS_CONFIG_FILE: /dev/null",
        ):
            self.assertIn(required, native)
        self.assertNotIn("continue-on-error", native)
        install, acceptance = native.split("      - name: Native browser acceptance", 1)
        self.assertIn("playwright install --with-deps chromium", install)
        self.assertNotIn("pip install", acceptance)
        self.assertNotIn("playwright install", acceptance)
        harness = (ROOT / "tests/browser-native/run.py").read_text()
        self.assertTrue((ROOT / "tests/browser-native/bridge.js").is_file())
        # The dedicated runner must reject empty or skipped success, not unittest's default.
        self.assertIn("result.wasSuccessful() and result.testsRun == 5 and not result.skipped", harness)
        self.assertIn("sys.addaudithook(block_python_egress)", harness)
        self.assertIn("self.context.route('**/*', route)", harness)
        self.assertIn("'--proxy-server=http://127.0.0.1:9'", harness)

    def test_native_failure_artifacts_are_fixture_only_and_short_lived(self):
        workflow = (ROOT / ".github/workflows/offline-safety.yml").read_text()
        upload = workflow.split("      - name: Upload fixture-only native browser failure evidence\n", 1)[1].split("\n  infrastructure:\n", 1)[0]
        self.assertIn("if: ${{ failure() }}", upload)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", upload)
        paths = upload.split("          path: |\n", 1)[1].split("          if-no-files-found:", 1)[0]
        self.assertEqual([line.strip() for line in paths.splitlines()], [
            "${{ runner.temp }}/frame-browser-native/evidence.json",
            "${{ runner.temp }}/frame-browser-native/test_*.zip",
        ])
        self.assertIn("retention-days: 3", upload)
        self.assertIn("include-hidden-files: false", upload)

    def test_js_browser_gate_provisions_its_python_fixture_dependency(self):
        workflow = (ROOT / ".github/workflows/offline-safety.yml").read_text()
        javascript = workflow.split("\n  javascript:\n", 1)[1].split("\n  browser-native:\n", 1)[0]
        setup = javascript.index("uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065")
        install = javascript.index('"${{ steps.python.outputs.python-path }}" -m pip install Pillow==11.1.0')
        browser = javascript.index("      - name: Browser client regressions\n")
        self.assertLess(setup, install)
        self.assertLess(install, browser)
        self.assertIn("id: python\n        with:\n          python-version: '3.12'", javascript[setup:install])
        browser_step = javascript[browser:].split("      - name: Dashboard DOM regressions", 1)[0]
        self.assertIn("PYTHON: ${{ steps.python.outputs.python-path }}", browser_step)
        self.assertIn("node --test tests/test_browser_cache.js", browser_step)
        self.assertNotIn("continue-on-error", javascript)

    def test_js_gates_require_real_nonempty_test_paths(self):
        workflow = (ROOT / ".github/workflows/offline-safety.yml").read_text()
        self.assertIn("test -s tests/test_browser_cache.js", workflow)
        self.assertIn("node --test tests/test_browser_cache.js", workflow)
        self.assertIn("test -n", workflow)
        self.assertIn("npm run test:web", workflow)
        self.assertTrue((ROOT / "tests/test_browser_cache.js").is_file())
        self.assertTrue(list((ROOT / "apps/frame-dash/tests/web").glob("*.test.js")))

    def test_shell_syntax_gate_parses_every_script(self):
        # `bash -n a.sh b.sh` parses only a.sh; the rest become positional
        # arguments, so a broken second script would pass silently.
        workflow = (ROOT / ".github/workflows/offline-safety.yml").read_text()
        for line in workflow.splitlines():
            stripped = line.strip()
            if stripped.startswith("bash -n "):
                self.assertEqual(len(stripped.split()), 3, f"bash -n given multiple files: {stripped}")
        self.assertIn("for script in apps/frame-dash/scripts/*.sh tools/deploy/*.sh", workflow)
        self.assertIn('bash -n "$script"', workflow)
        scripts = list((ROOT / "apps/frame-dash/scripts").glob("*.sh")) + list((ROOT / "tools/deploy").glob("*.sh"))
        self.assertGreaterEqual(len(scripts), 4)


if __name__ == "__main__":
    unittest.main()
