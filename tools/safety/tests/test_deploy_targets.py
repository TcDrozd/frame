"""Offline deployment guards: never invoke AWS, sudo, SAM deploy, or rsync."""
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("deploy_targets", ROOT / "tools/safety/deploy_targets.py")
assert spec is not None and spec.loader is not None
guards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guards)
web_spec = importlib.util.spec_from_file_location("check_web_config", ROOT / "tools/safety/check_web_config.py")
assert web_spec is not None and web_spec.loader is not None
web_guard = importlib.util.module_from_spec(web_spec)
web_spec.loader.exec_module(web_guard)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.target = guards.load_target(ROOT / "apps/frame-dash/samconfig.toml", "staging")

    def test_sam_quoted_empty_and_space_values_fail_closed(self):
        from unittest.mock import patch
        text = (ROOT / "apps/frame-dash/samconfig.toml").read_text()
        for before, after in [('LegacyManifestKey=""', 'LegacyManifestKey='), ('ScheduleExpression="rate(2 hours)"', 'ScheduleExpression=rate(2 hours)')]:
            with self.subTest(before=before), patch.object(guards.Path, "read_text", return_value=text.replace(before, after)):
                with self.assertRaises(ValueError):
                    guards.load_target(ROOT / "apps/frame-dash/samconfig.toml", "staging")

    def test_staging_is_isolated(self):
        guards.validate(self.target)
        self.assertEqual(self.target["parameters"]["ScheduleState"], "DISABLED")

    def test_every_staging_boundary_fails_closed(self):
        mutations = {
            "PhotosBucketName": "trevor-shared-photo-stream",
            "ManifestKey": "manifest.json",
            "LegacyManifestKey": "manifest.json",
            "CognitoDomainPrefix": "frame-dash-tcd",
            "ScheduleState": "ENABLED",
            "PrivateKeySsmName": "/frame-dash/cf-signing-key",
            "Environment": "production",
            "PhotoUrlMode": "cloudfront",
            "AutoPublishMode": "window",
        }
        for key, value in mutations.items():
            with self.subTest(key=key):
                target = copy.deepcopy(self.target)
                target["parameters"][key] = value
                with self.assertRaises(ValueError):
                    guards.validate(target)
        for key in self.target["parameters"]:
            with self.subTest(missing=key):
                target = copy.deepcopy(self.target)
                del target["parameters"][key]
                with self.assertRaises(ValueError):
                    guards.validate(target)

    def test_wrong_stack_region_profile_and_unknown_env_rejected(self):
        for key, value in [("stack_name", "frame-dash"), ("region", "eu-west-1"), ("profile", "sunflower-dev"), ("env", "typo")]:
            target = copy.deepcopy(self.target)
            target[key] = value
            with self.assertRaises(ValueError):
                guards.validate(target)

    def test_production_requires_exact_intent_and_never_dev_manifest(self):
        for env in ("default", "prod"):
            target = guards.load_target(ROOT / "apps/frame-dash/samconfig.toml", env)
            for intent in (None, "yes", "staging"):
                with self.assertRaises(ValueError):
                    guards.validate(target, intent)
            guards.validate(target, "frame-dash")
            target["parameters"]["ManifestKey"] = "manifest.dev.json"
            with self.assertRaises(ValueError):
                guards.validate(target, "frame-dash")

    def stack_fixture(self):
        # Synthetic unit-test fixture only; never presented as cloud evidence.
        values = {
            "WebBucketName": "frame-dash-staging-web-084683516815",
            "DistributionId": "E123456789TEST",
            "DashboardUrl": "https://exampletest.cloudfront.net",
            "ApiUrl": "https://stagingtest.execute-api.us-east-1.amazonaws.com",
            "CognitoDomain": "https://frame-dash-staging-084683516815.auth.us-east-1.amazoncognito.com",
            "UserPoolClientId": "staging-test-client",
            "ManifestUrl": "https://frame-dash-staging-photos-084683516815.s3.us-east-1.amazonaws.com/staging/manifest.json",
        }
        return {
            "StackName": "frame-dash-staging",
            "StackId": "arn:aws:cloudformation:us-east-1:084683516815:stack/frame-dash-staging/test-id",
            "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in guards.STAGING.items()],
            "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in values.items()],
        }

    def test_actual_output_targets_must_match(self):
        stack = self.stack_fixture()
        guards.validate_outputs(self.target, stack)
        for key, value in (("StackName", "frame-dash"), ("StackId", "arn:aws:cloudformation:us-east-1:999999999999:stack/frame-dash-staging/test-id"), ("Outputs", []), ("Parameters", [])):
            bad = copy.deepcopy(stack)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                guards.validate_outputs(self.target, bad)
        for key, value in (("WebBucketName", "frame-dash-web-084683516815"), ("DistributionId", "None"), ("DashboardUrl", "https://evil.test")):
            bad = copy.deepcopy(stack)
            next(o for o in bad["Outputs"] if o["OutputKey"] == key)["OutputValue"] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                guards.validate_outputs(self.target, bad)

    def test_staging_web_rejects_checked_in_production_config(self):
        with self.assertRaises(ValueError):
            web_guard.check(ROOT / "apps/frame-dash/web", self.stack_fixture())

    def test_stack_names_and_schedule_are_not_fixed_to_production(self):
        template = (ROOT / "apps/frame-dash/template.yaml").read_text()
        self.assertIn("TableName: !Sub ${AWS::StackName}-playlists", template)
        self.assertIn("BucketName: !Sub ${AWS::StackName}-web-${AWS::AccountId}", template)
        self.assertIn("State: !Ref ScheduleState", template)
        self.assertIn("RuleCondition: !Equals [!Ref Environment, staging]", template)
        for parameter in ("PhotosBucketName", "ManifestKey", "LegacyManifestKey", "CognitoDomainPrefix", "ScheduleState", "AutoPublishMode", "PhotoUrlMode", "CloudFrontPublicKeyPem", "PrivateKeySsmName"):
            self.assertIn(f"Assert: !Equals [!Ref {parameter},", template)

    def test_entrypoints_refuse_before_external_commands(self):
        scripts = ["apps/frame-dash/scripts/deploy_web.sh", "apps/frame-dash/scripts/deploy_stack.sh", "tools/deploy/deploy_client.sh", "tools/deploy/deploy_portal.sh"]
        import os
        import tempfile
        # A regression must fail the test, not accidentally call AWS or sudo.
        with tempfile.TemporaryDirectory() as directory:
            stubs = Path(directory)
            marker = stubs / "external-command-attempted"
            for command in ("aws", "sam", "sudo", "rsync", "ssh", "curl"):
                stub = stubs / command
                stub.write_text('#!/bin/sh\ntouch "$FRAME_TEST_EXTERNAL_MARKER"\nexit 97\n')
                stub.chmod(0o755)
            for script in scripts:
                with self.subTest(script=script):
                    env = {**os.environ, "FRAME_PRODUCTION_INTENT": "", "FRAME_ENV": "", "STACK": "", "PATH": str(stubs) + os.pathsep + os.environ["PATH"], "FRAME_TEST_EXTERNAL_MARKER": str(marker)}
                    result = subprocess.run(["bash", str(ROOT / script)], env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("intent", result.stderr.lower())
                    self.assertFalse(marker.exists(), "An external command ran before validation")


if __name__ == "__main__":
    unittest.main()
