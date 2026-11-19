"""Credential-free source-template IAM contracts (stdlib only).

These intentionally test exact SAM statements, not an IAM simulator. SAM lint
and local translator expansion separately validate syntax/generated policies.
No evidence here establishes live bucket policy or HEAD authorization behavior.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "apps/frame-dash/template.yaml"


def function_block(text, name):
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \w+:|\Z)", text, re.M | re.S)
    if not match:
        raise AssertionError(f"Missing function {name}")
    return match.group(1)


def statements(block):
    return dict(re.findall(
        r"^            - Sid: (\w+)\n(.*?)(?=^            - Sid:|^        - |\Z)",
        block, re.M | re.S))


class PublicationIAMTests(unittest.TestCase):
    def setUp(self):
        self.template = TEMPLATE.read_text()

    def test_both_publishers_have_exact_canonical_and_staged_get_put(self):
        for name in ("ApiFunction", "ScheduledFunction"):
            with self.subTest(function=name):
                block = function_block(self.template, name)
                grants = statements(block)
                self.assertEqual(set(grants), {
                    "ListPhotos", "ListCanonicalManifest", "PublicationTransaction",
                    "ReadPhotos", "ReadWriteManifest", "ReadSigningKey", "DecryptSigningKey"})
                manifest = grants["ReadWriteManifest"]
                self.assertIn("Effect: Allow", manifest)
                self.assertEqual(re.findall(r"- (s3:\w+)", manifest),
                                 ["s3:GetObject", "s3:PutObject"])
                self.assertEqual(re.findall(r"!Sub (arn:aws:s3:::[^\n]+)", manifest), [
                    "arn:aws:s3:::${PhotosBucketName}/${ManifestKey}",
                    "arn:aws:s3:::${PhotosBucketName}/${ManifestKey}.publications/*",
                    "arn:aws:s3:::${PhotosBucketName}/${LegacyManifestKey}"])
                self.assertIn("- HasLegacyManifestKey", manifest)
                self.assertIn("- !Ref AWS::NoValue", manifest)
                self.assertNotRegex(block, r"s3:(?:Delete\w*|\*|PutObjectAcl)")
                self.assertNotIn("Principal:", block)
                self.assertIn("Action: s3:GetObject", grants["ReadPhotos"])
                self.assertIn("Resource: !Sub arn:aws:s3:::${PhotosBucketName}/${PhotosPrefix}*",
                              grants["ReadPhotos"])

    def test_bucket_listing_is_exact_canonical_or_photo_prefix_only(self):
        for name in ("ApiFunction", "ScheduledFunction"):
            with self.subTest(function=name):
                grants = statements(function_block(self.template, name))
                lists = [s for s in grants.values() if "Action: s3:ListBucket" in s]
                self.assertEqual(len(lists), 2)
                for statement in lists:
                    self.assertIn("Resource: !Sub arn:aws:s3:::${PhotosBucketName}\n", statement)
                    self.assertIn("Condition:", statement)
                    self.assertNotIn("IfExists", statement)
                self.assertIn("StringEquals:\n                  s3:prefix: !Ref ManifestKey",
                              grants["ListCanonicalManifest"])
                self.assertIn("StringLike:\n                  s3:prefix: !Sub ${PhotosPrefix}*",
                              grants["ListPhotos"])

    def test_transaction_actions_are_table_scoped_and_crud_is_preserved(self):
        for name in ("ApiFunction", "ScheduledFunction"):
            with self.subTest(function=name):
                block = function_block(self.template, name)
                transaction = statements(block)["PublicationTransaction"]
                self.assertEqual(re.findall(r"- (dynamodb:\w+)", transaction),
                                 ["dynamodb:PutItem", "dynamodb:ConditionCheckItem"])
                self.assertIn("Resource: !GetAtt PlaylistTable.Arn", transaction)
                self.assertNotIn("*", transaction)
                self.assertIn("- DynamoDBCrudPolicy:\n            TableName: !Ref PlaylistTable", block)
        self.assertNotIn("TimeToLiveSpecification:", self.template)

    def test_staging_roles_have_no_fixed_production_targets(self):
        for name in ("ApiFunction", "ScheduledFunction"):
            block = function_block(self.template, name)
            self.assertNotIn("trevor-shared-photo-stream", block)
            self.assertNotIn("frame-dash-playlists", block)
            self.assertNotIn("parameter/frame-dash/", block)
            for s3_resource in re.findall(r"!Sub (arn:aws:s3:::[^\n]+)", block):
                self.assertTrue(s3_resource.startswith("arn:aws:s3:::${PhotosBucketName}"))
        self.assertIn("TableName: !Sub ${AWS::StackName}-playlists", self.template)
        self.assertIn("State: !Ref ScheduleState", function_block(self.template, "ScheduledFunction"))
        for assertion in (
            "!Equals [!Ref PhotosBucketName, frame-dash-staging-photos-084683516815]",
            "!Equals [!Ref ManifestKey, staging/manifest.json]",
            '!Equals [!Ref LegacyManifestKey, ""]',
            "!Equals [!Ref ScheduleState, DISABLED]",
            "!Equals [!Ref PrivateKeySsmName, /frame-dash-staging/cf-signing-key]",
        ):
            self.assertIn("Assert: " + assertion, self.template)
        self.assertRegex(self.template, r"Environment:\n    Type: String\n    Default: production")
        self.assertRegex(self.template, r"ScheduleState:\n    Type: String\n    Default: ENABLED")

    def test_publications_cdn_behavior_is_signed_and_precedes_public_manifest(self):
        distribution = function_block(self.template, "PhotosDistribution")
        behaviors = distribution.split("        CacheBehaviors:\n", 1)[1]
        private = behaviors.split("          - PathPattern: m/*", 1)[0]
        self.assertIn('          - PathPattern: "*.publications/*"', private)
        self.assertIn("TrustedKeyGroups: [!Ref PhotosKeyGroup]", private)
        self.assertIn("4135ea2d-6df8-44a3-9df3-4b5a84be39ad", private)
        self.assertEqual(re.findall(r"- PathPattern: (.+)", behaviors),
                         ['"*.publications/*"', "m/*"])

    def test_template_does_not_claim_to_manage_photo_bucket_controls(self):
        # Staging/canonical privacy and out-of-band writer protection require a
        # separate approved pre-existing bucket policy/retention review.
        self.assertNotIn("Bucket: !Ref PhotosBucketName", self.template)
        self.assertNotIn("s3:DeleteObject", self.template)


if __name__ == "__main__":
    unittest.main()
