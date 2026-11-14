#!/usr/bin/env python3
"""Offline native Chromium acceptance; no package installs or remote resources."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def png(rgb):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' + bytes(rgb))) + chunk(b'IEND', b''))


RED = png((255, 0, 0))
BLUE = png((0, 0, 255))
# Image MIME and PNG signature are valid; native decoder must reject the body.
CORRUPT = RED[:33] + b'broken IDAT body'


def block_python_egress(event, args):
    if event == 'socket.connect':
        sock, address = args
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = host == 'localhost'
            if not allowed:
                raise RuntimeError(f'Nonloopback Python connection forbidden: {host}')


class Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = None
        cls.browser = None
        cls.server = None
        cls.evidence = {'tests': [], 'source': str(ROOT / 'apps/client/app.js')}
        source = (ROOT / 'apps/client/app.js').read_text()
        cls.evidence['source_sha256'] = hashlib.sha256(source.encode()).hexdigest()
        # Exclude startup invocation ONLY; execute all production functions intact.
        marker = '\nif (document.readyState === "loading") {'
        if source.count(marker) != 1:
            raise RuntimeError('Startup boundary changed; review source extraction, refusing to guess')
        cls.script = source.split(marker)[0] + '\n' + (HERE / 'bridge.js').read_text()
        cls.evidence['startup_exclusion'] = source[source.index(marker):]

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                status, mime, body = 200, 'image/png', b''
                if path == '/':
                    mime = 'text/html'
                    body = b'<!doctype html><meta charset="utf-8"><div id="status"></div><script src="/test-client.js"></script>'
                elif path == '/test-client.js':
                    mime, body = 'text/javascript', cls.script.encode()
                elif path == '/fixture/red.png':
                    body = RED
                elif path == '/fixture/blue.png':
                    body = BLUE
                elif path == '/fixture/corrupt.png':
                    body = CORRUPT
                else:
                    status, mime, body = 503, 'text/plain', b'controlled acquisition failure'
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        executable = os.environ.get('FRAME_BROWSER_EXECUTABLE')
        bundled = Path(cls.pw.chromium.executable_path)
        if executable:
            launch = {'executable_path': executable}
        elif bundled.exists():
            launch = {'executable_path': str(bundled)}
        elif Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome').exists():
            launch = {'executable_path': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'}
        else:
            raise RuntimeError(f'No installed Chromium found (bundled: {bundled}); no downloads attempted')
        cls.evidence['executable'] = launch['executable_path']
        cls.browser = cls.pw.chromium.launch(headless=True, **launch, args=[
            '--disable-background-networking', '--disable-component-update', '--disable-sync',
            '--no-first-run', '--disable-default-apps', '--disable-quic',
            '--proxy-server=http://127.0.0.1:9',
            '--proxy-bypass-list=127.0.0.1;localhost;[::1]',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
        ])
        cls.evidence['browser_version'] = cls.browser.version
        cls.evidence['origin'] = cls.origin
        print(f'Native browser: {cls.browser.version}; executable: {launch["executable_path"]}', flush=True)
        print(f'Production source SHA256: {cls.evidence["source_sha256"]}', flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.cleanup_class()

    @classmethod
    def cleanup_class(cls):
        if cls.browser:
            cls.browser.close()
            cls.browser = None
        if cls.pw:
            cls.pw.stop()
            cls.pw = None
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
            cls.server = None
        (ARTIFACTS / 'evidence.json').write_text(json.dumps(cls.evidence, indent=2) + '\n')

    def setUp(self):
        assert self.browser is not None
        self.context = self.browser.new_context(service_workers='block')
        self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.denied = []

        def route(request_route):
            if urlsplit(request_route.request.url).netloc == urlsplit(self.origin).netloc and request_route.request.url.startswith(self.origin + '/'):
                request_route.continue_()
            else:
                self.denied.append(request_route.request.url)
                request_route.abort('blockedbyclient')

        self.context.route('**/*', route)
        self.context.route_web_socket('**/*', lambda ws: ws.close())
        self.page = self.context.new_page()
        self.page.set_default_timeout(15000)
        self.page.goto(self.origin)
        self.page.evaluate('nativeAcceptance.ready')
        self.record: dict[str, Any] = {'name': self._testMethodName}
        self.evidence['tests'].append(self.record)

    def tearDown(self):
        self.record['blocked_urls'] = self.denied
        self.context.tracing.stop(path=ARTIFACTS / f'{self._testMethodName}.zip')
        self.context.close()

    def manifest(self, version, entries):
        return {'schema': 2, 'version': version, 'mode': 'sync', 'start_epoch': 100,
                'slide_seconds': 10, 'photos': [
                    {'id': key.rsplit('/', 1)[-1], 'key': key, 'etag': etag,
                     'url': self.origin + '/fixture/' + fixture}
                    for key, etag, fixture in entries]}

    def sync(self, manifest):
        return self.page.evaluate('(m) => nativeAcceptance.sync(m)', manifest)

    def snapshot(self):
        return self.page.evaluate('nativeAcceptance.snapshot()')

    def reload_snapshot(self):
        self.page.reload()
        self.page.evaluate('nativeAcceptance.ready')
        snapshot = self.snapshot()
        self.record.setdefault('reload_snapshots', []).append(snapshot)
        return snapshot

    def seed(self):
        m = self.manifest('old', [('shared/a.png', 'old', 'red.png'), ('retired/a.png', 'old', 'red.png')])
        result = self.sync(m)
        self.assertTrue(result.get('result', {}).get('changed'), result)
        before = self.snapshot()
        self.assertEqual(len(before['photos']), 2)
        for photo in before['photos']:
            self.assertEqual(photo['blob']['bytes'], list(RED))
        self.record['baseline_snapshot'] = before
        return before

    def test_failed_acquisition_persists_old_window_after_reload(self):
        before = self.seed()
        incoming = self.manifest('new', [('shared/a.png', 'new', 'blue.png'), ('new/b.png', 'new', 'missing.png')])
        failed = self.sync(incoming)
        self.record['failed_sync'] = failed
        self.assertFalse(failed.get('result', {}).get('changed'), failed)
        self.assertEqual(failed['result']['ok'], 1)
        self.assertEqual(failed['result']['fail'], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.reload_snapshot(), before)
        self.record['persisted_old_window_verified'] = True
        # Recovery proves the prior failed incoming version was not marked accepted.
        incoming['photos'][1]['url'] = self.origin + '/fixture/blue.png'
        self.assertTrue(self.sync(incoming)['result']['changed'])
        after = self.reload_snapshot()
        self.assertEqual([p['id'] for p in after['photos']], ['new/b.png', 'shared/a.png'])
        self.assertTrue(all(p['blob']['bytes'] == list(BLUE) for p in after['photos']))
        self.record['retry_promoted_and_pruned'] = True

    def test_native_decoder_rejects_image_mime_corruption(self):
        before = self.seed()
        red = self.page.evaluate('(url) => nativeAcceptance.decode(url)', self.origin + '/fixture/red.png')
        corrupt = self.page.evaluate('(url) => nativeAcceptance.decode(url)', self.origin + '/fixture/corrupt.png')
        self.assertEqual(red, {'decoded': True, 'width': 1, 'height': 1})
        self.assertFalse(corrupt['decoded'], corrupt)
        self.record['native_decode_valid'] = red
        self.record['native_decode_corrupt'] = corrupt
        failed = self.sync(self.manifest('corrupt', [('shared/a.png', 'new', 'corrupt.png')]))
        self.record['corrupt_sync'] = failed
        self.assertFalse(failed.get('result', {}).get('changed'), failed)
        self.assertEqual(failed['result']['fail'], 1)
        self.assertEqual(self.reload_snapshot(), before)
        self.record['persisted_old_window_verified'] = True

    def test_native_image_element_fallback_rejects_corruption(self):
        # Select the production legacy-browser branch; Image.onload/onerror
        # and image decoding remain the real browser implementations.
        self.page.evaluate('window.createImageBitmap = undefined')
        before = self.seed()
        failed = self.sync(self.manifest('fallback-corrupt', [('shared/a.png', 'new', 'corrupt.png')]))
        self.record['corrupt_sync'] = failed
        self.assertFalse(failed.get('result', {}).get('changed'), failed)
        self.assertEqual(failed['result']['fail'], 1)
        self.assertEqual(self.reload_snapshot(), before)
        self.record['native_image_element_fallback_rejection_verified'] = True

    def test_native_abort_after_successful_writes_rolls_back_every_store(self):
        before = self.seed()
        self.page.evaluate('nativeAcceptance.armAbort()')
        incoming = self.manifest('abort', [('shared/a.png', 'new', 'blue.png'), ('new/b.png', 'new', 'blue.png')])
        failed = self.sync(incoming)
        evidence = self.page.evaluate('nativeAcceptance.abortEvidence')
        self.record.update({'aborted_sync': failed, 'native_transaction': evidence})
        self.assertIn('error', failed)
        self.assertTrue(evidence['aborted'], evidence)
        events = evidence['events']
        self.assertEqual(sum(e['store'] == 'photos' and e['kind'] == 'put' for e in events), 2)
        self.assertIn({'kind': 'delete', 'store': 'photos', 'key': 'retired/a.png', 'success': True}, events)
        for key in ['manifest', 'manifest_sig', 'last_sync', 'last_check']:
            self.assertIn({'kind': 'put', 'store': 'meta', 'key': key, 'success': True}, events)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.reload_snapshot(), before)
        self.record['rollback_bytes_pruning_all_metadata_after_reload'] = True
        self.assertTrue(self.sync(incoming)['result']['changed'])
        self.assertEqual([p['id'] for p in self.reload_snapshot()['photos']], ['new/b.png', 'shared/a.png'])
        self.record['retry_after_abort_succeeded'] = True

    def test_external_requests_blocked(self):
        result = self.page.evaluate('''async () => {
          try { await fetch('https://offline-egress-probe.invalid/'); return 'unexpected'; }
          catch (_) { return 'blocked'; }
        }''')
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.denied, ['https://offline-egress-probe.invalid/'])
        self.record['external_fetch_blocked_before_network'] = True


if __name__ == '__main__':
    import sys
    sys.addaudithook(block_python_egress)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, help='Scratch output directory (default: ~/.hermes/cache/scratch/frame-browser-native-*)')
    args = parser.parse_args()
    scratch = Path.home() / '.hermes/cache/scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    ARTIFACTS = args.artifacts or Path(tempfile.mkdtemp(prefix='frame-browser-native-', dir=scratch))
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    print(f'Artifacts: {ARTIFACTS}', flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Acceptance)
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        Acceptance.evidence['summary'] = {'run': result.testsRun, 'failures': len(result.failures),
                                         'errors': len(result.errors), 'skipped': len(result.skipped)}
    finally:
        Acceptance.cleanup_class()
    raise SystemExit(0 if result.wasSuccessful() and result.testsRun == 5 and not result.skipped else 1)
