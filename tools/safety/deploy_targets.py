#!/usr/bin/env python3
"""Offline, fail-closed deployment target validation; no SDK or credential access."""
import argparse
import json
from pathlib import Path
import re
import shlex
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "apps/frame-dash/samconfig.toml"
STAGING = {
    "Environment": "staging",
    "PhotosBucketName": "frame-dash-staging-photos-084683516815",
    "PhotosPrefix": "photos/",
    "ManifestKey": "staging/manifest.json",
    "LegacyManifestKey": "",
    "CognitoDomainPrefix": "frame-dash-staging-084683516815",
    "ScheduleState": "DISABLED",
    "ScheduleExpression": "rate(2 hours)",
    "AutoPublishMode": "off",
    "AutoWindowSize": "50",
    "PhotoUrlMode": "presign",
    "CloudFrontPublicKeyPem": "",
    "PrivateKeySsmName": "/frame-dash-staging/cf-signing-key",
    "PresignExpirySeconds": "21600",
    "SignedUrlTtlSeconds": "7776000",
}


def load_target(path, env):
    config = tomllib.loads(Path(path).read_text())
    if env not in ("default", "prod", "staging") or env not in config:
        raise ValueError("Unknown environment; choose staging or prod explicitly")
    section = config[env]
    # SAM inherits default globals; resolve it exactly, then validate.
    target = dict(config.get("default", {}).get("global", {}).get("parameters", {}))
    target.update(section.get("global", {}).get("parameters", {}))
    deploy = section.get("deploy", {}).get("parameters", {})
    overrides = deploy.get("parameter_overrides", [])
    tokens = shlex.split(overrides) if isinstance(overrides, str) else overrides
    parameters = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError("Malformed parameter override")
        key, value = token.split("=", 1)
        if isinstance(overrides, list) and value.startswith(('"', "'")):
            parsed = shlex.split(value)
            if len(parsed) != 1:
                raise ValueError("Malformed quoted parameter override")
            value = parsed[0]
        elif isinstance(overrides, list) and (not value or any(c.isspace() for c in value)):
            raise ValueError("SAM list values containing whitespace or empty strings require explicit quoting")
        if key in parameters:
            raise ValueError("Duplicate parameter override: " + key)
        parameters[key] = value
    target.update(env=env, parameters=parameters)
    return target


def validate(target, production_intent=None):
    env = target.get("env")
    if env not in ("default", "prod", "staging"):
        raise ValueError("Unknown environment")
    expected_stack = "frame-dash-staging" if env == "staging" else "frame-dash"
    expected_profile = "frame-dash-staging-deploy" if env == "staging" else "frame-dash-deploy"
    for key, expected in (("stack_name", expected_stack), ("profile", expected_profile), ("region", "us-east-1")):
        if target.get(key) != expected:
            raise ValueError(f"Unsafe {key}: expected {expected}")
    params = target.get("parameters", {})
    if env == "staging":
        if params != STAGING:
            raise ValueError("Staging requires the complete, exact isolated parameter set")
    else:
        if production_intent != "frame-dash":
            raise ValueError("Production intent required: FRAME_PRODUCTION_INTENT=frame-dash")
        # Leave existing production parameters untouched (SAM UsePreviousValue).
        if params != {"ManifestKey": "manifest.json"}:
            raise ValueError("Production must pin manifest.json only; review persisted parameters separately")
    return target


def validate_outputs(target, stack):
    """Validate actual describe-stacks response before the first web write."""
    if stack.get("StackName") != target["stack_name"]:
        raise ValueError("Stack output identity mismatch")
    arn = stack.get("StackId", "")
    expected = f"arn:aws:cloudformation:us-east-1:084683516815:stack/{target['stack_name']}/"
    if not arn.startswith(expected):
        raise ValueError("Stack account/region mismatch")
    params = {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters", [])}
    if target["env"] == "staging":
        if any(params.get(k) != v for k, v in STAGING.items()):
            raise ValueError("Deployed staging parameters do not match isolation contract")
    elif params.get("ManifestKey") != "manifest.json" or params.get("PhotosBucketName") != "trevor-shared-photo-stream":
        raise ValueError("Production stack parameters mismatch")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    expected_bucket = f"{target['stack_name']}-web-084683516815"
    if outputs.get("WebBucketName") != expected_bucket:
        raise ValueError("Web bucket is not stack-scoped")
    if not re.fullmatch(r"[A-Z0-9]{8,32}", outputs.get("DistributionId", "")):
        raise ValueError("Missing/malformed distribution ID")
    if not re.fullmatch(r"https://[a-z0-9]+\.cloudfront\.net", outputs.get("DashboardUrl", "")):
        raise ValueError("Missing/malformed dashboard URL")
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=["staging", "prod", "default"])
    parser.add_argument("--stack-json", type=Path)
    args = parser.parse_args()
    import os
    try:
        target = validate(load_target(CONFIG, args.env), os.environ.get("FRAME_PRODUCTION_INTENT"))
        if args.stack_json:
            stacks = json.loads(args.stack_json.read_text())["Stacks"]
            if len(stacks) != 1:
                raise ValueError("Expected exactly one stack")
            outputs = validate_outputs(target, stacks[0])
            for key in ("WebBucketName", "DistributionId", "DashboardUrl"):
                print(outputs[key])
        else:
            print(f"Validated {args.env}: {target['stack_name']} ({target['region']})")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Target refused: {exc}\n")


if __name__ == "__main__":
    main()
