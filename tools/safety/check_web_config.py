#!/usr/bin/env python3
"""Fail closed if a static SPA configuration does not match stack outputs."""
import json
from pathlib import Path
import re
import sys


def check(source, stack):
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    text = (Path(source) / "js/config.js").read_text()
    # Support only the existing buildless CONFIG literal, never execute JS.
    expected = {
        "apiUrl": outputs["ApiUrl"],
        "cognitoDomain": outputs["CognitoDomain"],
        "clientId": outputs["UserPoolClientId"],
        "manifestUrl": outputs.get("CdnManifestUrl", outputs["ManifestUrl"]),
    }
    for field, value in expected.items():
        found = re.findall(r"^\s*" + field + r'\s*:\s*"([^"\n]+)"\s*,?\s*$', text, re.MULTILINE)
        if found != [value]:
            raise ValueError(f"Web config {field} must exactly match the target stack output")
    return True


if __name__ == "__main__":
    try:
        check(sys.argv[1], json.loads(Path(sys.argv[2]).read_text())["Stacks"][0])
    except (ValueError, KeyError, IndexError, OSError) as exc:
        sys.exit(f"Web target refused: {exc}")
