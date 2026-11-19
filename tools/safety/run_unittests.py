#!/usr/bin/env python3
"""Run an offline unittest suite, refusing empty or skipped acceptance checks."""
from pathlib import Path
import sys
import unittest
import ipaddress
import socket

# Tests may use loopback HTTP fixtures, but an unstubbed SDK call must never
# leave the runner. Dependency installation/build happens outside this runner.
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex

def _local_only(original):
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise RuntimeError(f"Offline suite blocked outbound connection to {host}")
        return original(sock, address)
    return connect

socket.socket.connect = _local_only(_original_connect)
socket.socket.connect_ex = _local_only(_original_connect_ex)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if len(sys.argv) != 2 or sys.argv[1] not in ("tests", "apps/frame-dash/tests", "tools/safety/tests",
                                             "tests/browser-native"):
    sys.exit("Specify a known offline test suite")
suite = unittest.defaultTestLoader.discover(str(ROOT / sys.argv[1]))
if suite.countTestCases() == 0:
    sys.exit("Refused: no tests discovered")
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print("Refused: skipped tests are not acceptance evidence", file=sys.stderr)
if result.expectedFailures or result.unexpectedSuccesses:
    print("Refused: expected-failure markers are not acceptance evidence", file=sys.stderr)
sys.exit(0 if result.wasSuccessful() and not result.skipped
         and not result.expectedFailures and not result.unexpectedSuccesses else 1)
