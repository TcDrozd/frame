import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

const initial = new JSDOM('<main id="app"></main><div id="toast" hidden></div>', { url: 'https://frame.test/#/playlist/p1' });
globalThis.window = initial.window;
globalThis.document = initial.window.document;
globalThis.FormData = initial.window.FormData;
const { api, toast } = await import('../../web/js/api.js');
const transport = { ...api };
const { confirmLeaveDraft } = await import('../../web/js/draft.js');
const { renderPlaylistEditor, renderPlaylists } = await import('../../web/js/views/playlists.js');
const { renderBrowse } = await import('../../web/js/views/browse.js');
const { renderStatus } = await import('../../web/js/views/status.js');
const calls = [];
let playlist, pages, confirmResult;
const clone = value => structuredClone(value);
const app = () => document.getElementById('app');
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setTimeout(resolve, 0)); };
function setup() {
  const dom = new JSDOM('<main id="app"></main><div id="toast" hidden></div>', { url: 'https://frame.test/#/playlist/p1' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.FormData = dom.window.FormData;
  calls.length = 0;
  confirmResult = true;
  globalThis.confirm = window.confirm = message => { calls.push(['CONFIRM', message]); return confirmResult; };
  playlist = { id: 'p1', revision: 3, name: 'Family', items: ['photos/a.jpg', 'photos/b.jpg'], settings: { mode: 'sync', slide_seconds: 15, start_epoch: 'now' } };
  pages = [{ prefix: 'photos/', folders: [], photos: [{ key: 'photos/a.jpg' }], next_token: null }];
  api.get = async path => {
    calls.push(['GET', path]);
    if (path === '/api/status') return { manifest_key: 'manifest.json' };
    if (path === '/api/playlists') return { playlists: [clone(playlist)] };
    if (path.startsWith('/api/browse?')) return clone(pages.shift());
    return clone(playlist);
  };
  api.post = async (path, body) => {
    calls.push(['POST', path, clone(body)]);
    if (path === '/api/preview-urls') return { urls: Object.fromEntries(body.keys.map(key => [key, 'https://photos.test/' + encodeURIComponent(key)])) };
    return { manifest: { photos: [] }, skipped_keys: [], version: `v1-${body.request_id}`, photo_count: 2, request_id: body.request_id, manifest_key: 'manifest.json', dry_run: !!body.dry_run };
  };
  api.put = async (path, body) => {
    calls.push(['PUT', path, clone(body)]);
    playlist = { ...playlist, ...clone(body), revision: playlist.revision + 1 };
    return clone(playlist);
  };
  api.del = async (path, body) => { calls.push(['DELETE', path, clone(body)]); };
}
function edit(name, value) {
  const el = document.querySelector(`[name="${name}"]`);
  el.value = value;
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
  el.dispatchEvent(new window.Event('change', { bubbles: true }));
}
const writes = () => calls.filter(c => ['PUT', 'DELETE'].includes(c[0]) || (c[0] === 'POST' && c[1].endsWith('/publish')));

test.afterEach(() => clearTimeout(toast._t));

test('stored quotes stay data in editor inputs, thumbnails, and browse attributes', async () => {
  setup();
  const payload = '\" autofocus onfocus=\"globalThis.pwned=1\" data-injected=\"yes';
  playlist.name = payload;
  playlist.settings.start_epoch = payload;
  api.post = async (path, body) => ({ urls: Object.fromEntries(body.keys.map(key => [key, 'https://photos.test/a\" onerror=\"globalThis.pwned=1'])) });
  await renderPlaylistEditor(app(), 'p1');
  assert.equal(document.querySelector('[name="name"]').value, payload);
  assert.equal(document.querySelector('[name="start_epoch"]').value, payload);
  assert.equal(document.querySelectorAll('[onfocus], [onerror], [autofocus], [data-injected]').length, 0);
  pages[0].photos = [{ key: 'photos/' + payload + '.jpg' }];
  await renderBrowse(app(), new URLSearchParams());
  assert.equal(document.querySelectorAll('[onfocus], [onerror], [autofocus], [data-injected]').length, 0);
});

test('all settings survive reorder and remove; controls have accessible names', async () => {
  setup();
  await renderPlaylistEditor(app(), 'p1');
  edit('name', 'Unsaved'); edit('mode', 'inventory'); edit('slide_seconds', '27'); edit('start_epoch', '1234567890');
  document.querySelector('[data-act="down"]').click();
  document.querySelector('[data-act="remove"]').click();
  assert.equal(document.querySelector('[name="name"]').value, 'Unsaved');
  assert.equal(document.querySelector('[name="mode"]').value, 'inventory');
  assert.equal(document.querySelector('[name="slide_seconds"]').value, '27');
  assert.equal(document.querySelector('[name="start_epoch"]').value, '1234567890');
  for (const button of document.querySelectorAll('[data-act]')) assert.ok(button.getAttribute('aria-label'));
  await document.getElementById('save').onclick();
  assert.deepEqual(calls.find(c => c[0] === 'PUT')[2], { expected_revision: 3, name: 'Unsaved', settings: { mode: 'inventory', slide_seconds: 27, start_epoch: 1234567890 }, items: ['photos/a.jpg'] });
});

test('preview sends a revision-bound draft without any save', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Draft');
  await document.getElementById('dry-run').onclick();
  assert.equal(calls.filter(c => c[0] === 'PUT').length, 0);
  const body = calls.find(c => c[0] === 'POST' && c[1].endsWith('/publish'))[2];
  assert.deepEqual(body, { dry_run: true, expected_revision: 3, draft: { name: 'Draft', settings: playlist.settings, items: playlist.items } });
  assert.equal(document.getElementById('preview').hidden, false);
});

test('cancelled publication never saves or publishes', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Draft'); confirmResult = false;
  await document.getElementById('publish').onclick();
  assert.deepEqual(writes(), []);
  assert.equal(document.querySelector('[name="name"]').value, 'Draft');
});

test('confirmed publication saves only after review then binds exact returned revision', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Reviewed');
  await document.getElementById('publish').onclick(); await settle();
  const significant = calls.filter(c => c[0] === 'CONFIRM' || c[0] === 'PUT' || (c[0] === 'POST' && c[1].endsWith('/publish')));
  assert.deepEqual(significant.map(c => c[0]), ['CONFIRM', 'PUT', 'POST']);
  assert.match(significant[0][1], /Reviewed/);
  assert.equal(significant[1][2].expected_revision, 3);
  assert.match(significant[2][2].request_id, /^[0-9a-f]{32}$/);
  assert.deepEqual(significant[2][2], { dry_run: false, restart: true, expected_revision: 4, request_id: significant[2][2].request_id });
});

test('epoch form values become strict integers in save, preview, and publish review', async () => {
  for (const value of ['0', '1700000000', '253402300799', ' now ']) {
    for (const action of ['save', 'dry-run', 'publish']) {
      setup(); await renderPlaylistEditor(app(), 'p1'); edit('start_epoch', value);
      await document.getElementById(action).onclick();
      const expected = value.trim() === 'now' ? 'now' : Number(value);
      const write = writes().find(c => c[0] === 'PUT' || c[2].dry_run);
      assert.ok(write, `${action}: ${value}`);
      assert.equal((write[2].draft || write[2]).settings.start_epoch, expected);
      if (action === 'publish') assert.match(calls.find(c => c[0] === 'CONFIRM')[1], new RegExp(`start: ${expected}`));
    }
  }
});

test('malformed epochs fail clearly before save, preview, or confirmation', async () => {
  for (const value of ['', ' ', '-1', '1.5', '1e3', '0x10', '+1', '123abc', 'NaN', 'Infinity', 'NOW', '253402300800', '9007199254740993']) {
    setup(); await renderPlaylistEditor(app(), 'p1'); edit('start_epoch', value);
    for (const action of ['save', 'dry-run', 'publish']) await document.getElementById(action).onclick();
    assert.deepEqual(writes(), [], value);
    assert.equal(calls.filter(c => c[0] === 'CONFIRM').length, 0, value);
    assert.match(document.getElementById('toast').textContent, /start epoch.*now.*integer.*253402300799/i, value);
  }
});

test('ambiguous publish retries identical request and revision without resaving', async () => {
  for (const status of [undefined, 500, 503]) {
    setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Reviewed');
    const post = api.post;
    let fail = true;
    api.post = async (path, body) => {
      if (!fail) return post(path, body);
      calls.push(['POST', path, clone(body)]);
      throw Object.assign(new Error('Lost acknowledgement'), { status });
    };
    await document.getElementById('publish').onclick();
    assert.match(document.getElementById('toast').textContent, /unknown|unconfirmed/i);
    assert.match(document.getElementById('publish').textContent, /retry/i);
    const first = clone(writes().find(c => c[0] === 'POST')[2]);
    assert.match(first.request_id, /^[0-9a-f]{32}$/);
    // Cancelling the retry must neither save nor consume the retained intent.
    confirmResult = false;
    await document.getElementById('publish').onclick();
    assert.equal(writes().length, 2);
    confirmResult = true; fail = false;
    await document.getElementById('publish').onclick();
    assert.equal(calls.filter(c => c[0] === 'PUT').length, 1);
    const publishes = writes().filter(c => c[0] === 'POST');
    assert.deepEqual(publishes[1][2], first);
    assert.match(calls.filter(c => c[0] === 'CONFIRM').at(-1)[1], /retry|without saving/i);
    // A later deliberate publish, even of identical contents, is a new intent.
    await document.getElementById('publish').onclick();
    const next = writes().filter(c => c[0] === 'POST').at(-1)[2];
    assert.notEqual(next.request_id, first.request_id);
    assert.equal(next.expected_revision, first.expected_revision + 1);
  }
});

test('edited draft or explicit save never reuses an ambiguous publication ID', async () => {
  for (const action of ['edit', 'save']) {
    setup(); await renderPlaylistEditor(app(), 'p1');
    const post = api.post;
    api.post = async (path, body) => { calls.push(['POST', path, clone(body)]); throw new TypeError('Network lost'); };
    await document.getElementById('publish').onclick();
    const first = clone(writes().find(c => c[0] === 'POST')[2]);
    if (action === 'edit') edit('name', 'Different intent');
    else await document.getElementById('save').onclick();
    api.post = post;
    await document.getElementById('publish').onclick();
    const next = writes().filter(c => c[0] === 'POST').at(-1)[2];
    assert.match(next.request_id, /^[0-9a-f]{32}$/);
    assert.notEqual(next.request_id, first.request_id);
    assert.ok(next.expected_revision > first.expected_revision);
  }
});

test('repeated lost acknowledgements and preview preserve pending intent and leave warning', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('start_epoch', '1700000000');
  const post = api.post;
  api.post = async (path, body) => {
    if (body.dry_run) return post(path, body);
    calls.push(['POST', path, clone(body)]);
    throw new TypeError('Lost acknowledgement');
  };
  await document.getElementById('publish').onclick();
  const event = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true, 'unconfirmed publish must warn even though draft was saved');
  await document.getElementById('dry-run').onclick();
  await document.getElementById('publish').onclick();
  await document.getElementById('publish').onclick();
  const publishes = writes().filter(c => c[0] === 'POST' && !c[2].dry_run);
  assert.equal(publishes.length, 3);
  for (const publish of publishes) assert.deepEqual(publish[2], publishes[0][2]);
  assert.equal(calls.filter(c => c[0] === 'PUT').length, 1);
  api.post = post;
  await document.getElementById('publish').onclick();
  const after = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(after);
  assert.equal(after.defaultPrevented, false);
});

test('reordering or a definite publish conflict requires a new intent and request ID', async () => {
  for (const action of ['reorder', 'conflict']) {
    setup(); await renderPlaylistEditor(app(), 'p1');
    const post = api.post;
    api.post = async (path, body) => {
      calls.push(['POST', path, clone(body)]);
      throw Object.assign(new Error('publish failed'), action === 'conflict' ? { status: 409 } : {});
    };
    await document.getElementById('publish').onclick();
    const first = clone(writes().find(c => c[0] === 'POST')[2]);
    if (action === 'reorder') document.querySelector('[data-act="down"]').click();
    assert.doesNotMatch(document.getElementById('publish').textContent, /retry/i);
    api.post = post;
    await document.getElementById('publish').onclick();
    const next = writes().filter(c => c[0] === 'POST').at(-1)[2];
    assert.notEqual(next.request_id, first.request_id);
    assert.equal(next.expected_revision, first.expected_revision + 1);
  }
});

test('stale editor save retains draft, warns, and never publishes', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Keep me');
  api.put = async () => { throw Object.assign(new Error('Revision conflict'), { status: 409 }); };
  await document.getElementById('publish').onclick();
  assert.equal(calls.filter(c => c[0] === 'POST' && c[1].endsWith('/publish')).length, 0);
  assert.equal(document.querySelector('[name="name"]').value, 'Keep me');
  assert.match(document.getElementById('toast').textContent, /conflict|changed/i);
});

test('publish conflict from a recovered earlier publication is reported truthfully', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'After recovery');
  const realPost = api.post;
  api.post = async (path, body) => {
    if (path.endsWith('/publish') && !body.dry_run) {
      throw Object.assign(new Error('recovered earlier publication; reload before publishing'), { status: 409 });
    }
    return realPost(path, body);
  };
  await document.getElementById('publish').onclick();
  const text = document.getElementById('toast').textContent;
  // The server completed an EARLIER pending publication during this request:
  // something may have changed on the frames, so never claim nothing was published.
  assert.doesNotMatch(text, /nothing was published/i);
  assert.match(text, /recovered earlier publication/);
  assert.match(text, /reload/i);
  assert.equal(document.querySelector('[name="name"]').value, 'After recovery');
});

test('plain revision conflict shows the server reason, not a fixed claim', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Mine');
  api.put = async () => { throw Object.assign(new Error('playlist revision changed'), { status: 409 }); };
  await document.getElementById('save').onclick();
  const text = document.getElementById('toast').textContent;
  assert.match(text, /playlist revision changed/);
  assert.match(text, /draft is kept/i);
  assert.doesNotMatch(text, /nothing was published/i);
});

test('dirty editor warns before unload and clears warning after save', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Dirty');
  const before = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(before);
  assert.equal(before.defaultPrevented, true);
  await document.getElementById('save').onclick();
  const after = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(after);
  assert.equal(after.defaultPrevented, false);
});

test('folder pagination accumulates and deduplicates folders and photos', async () => {
  setup(); pages = [
    { prefix: 'photos/', folders: ['photos/A/'], photos: [{ key: 'photos/a.jpg' }], next_token: 'next' },
    { prefix: 'photos/', folders: ['photos/B/'], photos: [{ key: 'photos/a.jpg' }, { key: 'photos/b.jpg' }], next_token: null }
  ];
  await renderBrowse(app(), new URLSearchParams());
  document.getElementById('load-more').click(); await settle();
  assert.deepEqual([...document.querySelectorAll('.folder')].map(el => el.textContent), ['📁 A', '📁 B']);
  assert.equal(document.querySelectorAll('.card').length, 2);
  assert.match(calls.filter(c => c[0] === 'GET' && c[1].startsWith('/api/browse?'))[1][1], /token=next/);
});

test('browse append binds fetched revision; conflict preserves selection with no retry', async () => {
  setup(); await renderBrowse(app(), new URLSearchParams());
  const cb = document.querySelector('.card input'); cb.checked = true; cb.dispatchEvent(new window.Event('change', { bubbles: true }));
  playlist.items = [];
  api.put = async (path, body) => { calls.push(['PUT', path, clone(body)]); throw Object.assign(new Error('Revision conflict'), { status: 409 }); };
  const pick = document.getElementById('playlist-pick'); pick.value = 'p1'; pick.dispatchEvent(new window.Event('change')); await settle();
  assert.deepEqual(calls.filter(c => c[0] === 'PUT').map(c => c[2]), [{ expected_revision: 3, items: ['photos/a.jpg'] }]);
  assert.equal(cb.checked, true);
  assert.match(document.getElementById('toast').textContent, /conflict|changed/i);
});

test('navigation cancellation retains draft and unload warning', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Stay');
  confirmResult = false;
  assert.equal(confirmLeaveDraft(), false);
  assert.equal(document.querySelector('[name="name"]').value, 'Stay');
  const event = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true);
  confirmResult = true;
  assert.equal(confirmLeaveDraft(), true);
  const after = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(after);
  assert.equal(after.defaultPrevented, false);
});

test('publish-time conflict keeps saved reviewed data and does not retry', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Reviewed');
  api.post = async (path, body) => {
    calls.push(['POST', path, clone(body)]);
    throw Object.assign(new Error('changed after save'), { status: 409 });
  };
  await document.getElementById('publish').onclick();
  assert.equal(calls.filter(c => c[0] === 'PUT').length, 1);
  assert.equal(calls.filter(c => c[0] === 'POST' && c[1].endsWith('/publish')).length, 1);
  assert.equal(document.querySelector('[name="name"]').value, 'Reviewed');
  assert.match(document.getElementById('toast').textContent, /conflict/i);
});

test('missing revision fails closed and invalid seconds do not send writes', async () => {
  setup(); delete playlist.revision; await renderPlaylistEditor(app(), 'p1');
  await document.getElementById('publish').onclick();
  await document.getElementById('save').onclick();
  assert.deepEqual(writes(), []);
  assert.match(document.getElementById('toast').textContent, /revision/i);
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('slide_seconds', '5.2');
  await document.getElementById('save').onclick();
  assert.deepEqual(writes(), []);
});

test('delete is confirmed and revision-bound; inactive activation uses publish review', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); confirmResult = false;
  await document.getElementById('delete').onclick();
  await document.getElementById('toggle-active').onclick();
  assert.deepEqual(writes(), []);
  confirmResult = true;
  await document.getElementById('delete').onclick();
  assert.deepEqual(calls.find(c => c[0] === 'DELETE'), ['DELETE', '/api/playlists/p1', { expected_revision: 3 }]);
});

test('preview is invalidated on edit; preview preserves dirty warning', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1'); edit('name', 'Draft');
  await document.getElementById('dry-run').onclick();
  const before = new window.Event('beforeunload', { cancelable: true }); window.dispatchEvent(before);
  assert.equal(before.defaultPrevented, true);
  edit('name', 'Changed again');
  assert.equal(document.getElementById('preview').hidden, true);
  assert.equal(calls.filter(c => c[0] === 'PUT').length, 0);
});

test('folder-only pages remain visible and deduplicate across three pages', async () => {
  setup(); pages = [
    { prefix: 'photos/', folders: ['photos/A/'], photos: [], next_token: '1' },
    { prefix: 'photos/', folders: ['photos/B/'], photos: [], next_token: '2' },
    { prefix: 'photos/', folders: ['photos/A/', 'photos/B/'], photos: [], next_token: null }
  ];
  await renderBrowse(app(), new URLSearchParams());
  document.getElementById('load-more').click(); await settle();
  document.getElementById('load-more').click(); await settle();
  assert.deepEqual([...document.querySelectorAll('.folder')].map(el => el.textContent), ['📁 A', '📁 B']);
  assert.equal(document.getElementById('load-more'), null);
  assert.equal(document.querySelectorAll('.card').length, 0);
});

test('delayed browse response cannot replace a newer editor', async () => {
  setup();
  const get = api.get;
  let release;
  api.get = path => path.startsWith('/api/browse?') ? new Promise(resolve => { release = resolve; }) : get(path);
  const pending = renderBrowse(app(), new URLSearchParams());
  await renderPlaylistEditor(app(), 'p1'); edit('name', 'Keep draft');
  release({ prefix: 'photos/', folders: [], photos: [], next_token: null });
  await pending;
  assert.equal(document.querySelector('[name="name"]').value, 'Keep draft');
});

test('actual app router restores cancelled hash navigation and later permits leaving', async () => {
  setup();
  document.body.insertAdjacentHTML('beforeend', '<button id="logout-btn" hidden>Logout</button><nav><a href="#/browse">Browse</a></nav>');
  // A synthetic local token only bypasses ensureLogin in this isolated DOM.
  // fetch is forbidden here: no Cognito/production authentication takes place.
  initial.window.sessionStorage.setItem('access_token', `test.${Buffer.from(JSON.stringify({ exp: 9999999999 })).toString('base64url')}.test`);
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('No network allowed in router test'); };
  try {
    await import('../../web/js/app.js'); await settle();
    edit('name', 'Keep route draft'); confirmResult = false;
    window.location.hash = '#/browse'; await settle();
    assert.equal(window.location.hash, '#/playlist/p1');
    assert.equal(document.querySelector('[name="name"]').value, 'Keep route draft');
    confirmResult = true;
    window.location.hash = '#/browse'; await settle();
    assert.ok(document.getElementById('photo-grid'));
  } finally {
    globalThis.fetch = fetch;
    initial.window.sessionStorage.clear();
  }
});



const oldId = 'a'.repeat(32), newId = 'b'.repeat(32);
function statusFixture() {
  return { active: { playlist_id: 'p1', playlist_name: 'Published family', draft_revision: 9,
    published_revision: 4, snapshot: { id: 'p1', revision: 4, name: 'Published family', items: ['photos/a.jpg'] },
    last_version: `v-old-${oldId}`, last_published_at: '2026-01-01', resolved_start_epoch: 0 },
    manifest_key: 'manifest.json', manifest_head: { publication_id: oldId, size: 123, last_modified: '2026-01-01' },
    auto_publish: { mode: 'off' }, presign_expiry_seconds: 3600 };
}
const statusField = name => [...document.querySelectorAll('dt')].find(el => el.textContent === name)?.nextElementSibling;
test('status separates selection, frozen snapshot, pending reservation and observed canonical ID', async () => {
  setup(); const status = statusFixture();
  status.pending_publication = { request_id: newId, playlist_id: 'p1', published_revision: 9 };
  status.active.publication_pending = true; status.manifest_head.publication_id = newId;
  api.get = async () => clone(status);
  await renderStatus(app());
  assert.match(statusField('Selection').textContent, /Published family/);
  assert.match(statusField('Last published snapshot').textContent, /Published family.*revision 4/);
  assert.match(statusField('Pending publication').textContent, new RegExp(newId));
  assert.match(statusField('Observed canonical publication ID').textContent, new RegExp(newId));
  assert.match(document.querySelector('[role="alert"]').textContent, /pending.*not.*completed/i);
  assert.match(document.querySelector('[role="alert"]').textContent, /differ|diverg/i);
  status.pending_publication = null; status.active.publication_pending = false;
  await renderStatus(app());
  assert.match(document.querySelector('[role="alert"]').textContent, /differ|diverg/i);
});
test('clear with auto off shows retained show and frozen epoch, not auto counters or never', async () => {
  setup(); const status = statusFixture();
  status.active = { source: 'auto', previous_snapshot: status.active.snapshot,
    previous_version: status.active.last_version, previous_resolved_start_epoch: 0 };
  api.get = async () => clone(status); await renderStatus(app());
  assert.match(statusField('Selection').textContent, /none.*refresh is idle/i);
  assert.match(statusField('Last published snapshot').textContent, /Published family.*retained/i);
  assert.match(statusField('Last published').textContent, /v-old-/);
  assert.match(statusField('Playback anchored at').textContent, /epoch 0/);
  assert.doesNotMatch(app().textContent, /never|undefined|auto window —|of photos/);
});
test('status renders manifest observation errors separately from confirmed absence', async () => {
  setup(); const status = statusFixture(); status.manifest_head = null;
  status.manifest_observation = { state: 'error', code: '403' };
  api.get = async () => clone(status); await renderStatus(app());
  assert.match(statusField('Manifest object').textContent, /observation failed.*403/i);
  assert.doesNotMatch(statusField('Manifest object').textContent, /missing|not found|absent/i);
  status.manifest_observation = { state: 'missing', code: '404' };
  await renderStatus(app());
  assert.match(statusField('Manifest object').textContent, /not found.*404/i);
});

test('deleted draft keeps published snapshot name without an editor link', async () => {
  setup(); const status = statusFixture(); status.active.draft_revision = null;
  api.get = async () => clone(status); await renderStatus(app());
  assert.match(app().textContent, /Published family/);
  assert.equal(app().querySelector('a[href="#/playlist/p1"]'), null);
});
test('actual HTTP 200 unreadable and incomplete ACKs retain exact retry key and saved revision', async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const malformed of ['unreadable', {}, { version: 'v1', photo_count: 2, skipped_keys: [] }, null, 'wrong-id', 'wrong-target', 'wrong-count', 'wrong-skipped', 'preview']) {
      setup(); await renderPlaylistEditor(app(), 'p1');
      api.post = transport.post;
      let fail = true;
      globalThis.fetch = async (url, options) => {
        const body = JSON.parse(options.body); calls.push(['POST', url, body]);
        return { status: 200, ok: true, json: async () => {
          const valid = { version: `v-${body.request_id}`, request_id: body.request_id, manifest_key: 'manifest.json', dry_run: false, photo_count: 2, skipped_keys: [] };
          if (fail) {
            if (malformed === 'unreadable') throw new SyntaxError('truncated JSON');
            if (malformed === 'wrong-id') return { ...valid, request_id: oldId };
            if (malformed === 'wrong-target') return { ...valid, manifest_key: 'other.json' };
            if (malformed === 'wrong-count') return { ...valid, photo_count: '2' };
            if (malformed === 'wrong-skipped') return { ...valid, skipped_keys: null };
            if (malformed === 'preview') return { ...valid, dry_run: true };
            return malformed;
          }
          return valid;
        } };
      };
      await document.getElementById('publish').onclick();
      assert.match(document.getElementById('toast').textContent, /outcome unknown/i);
      assert.match(document.getElementById('publish').textContent, /retry/i);
      const first = clone(writes().filter(c => c[0] === 'POST').at(-1)[2]);
      fail = false; await document.getElementById('publish').onclick();
      assert.equal(calls.filter(c => c[0] === 'PUT').length, 1);
      assert.deepEqual(writes().filter(c => c[0] === 'POST').at(-1)[2], first);
    }
  } finally { globalThis.fetch = originalFetch; }
});
test('successful ACK publication ID survives a failed status refresh', async () => {
  setup(); await renderPlaylistEditor(app(), 'p1');
  api.get = async () => { throw new Error('Status unavailable'); };
  await document.getElementById('publish').onclick();
  const id = writes().find(c => c[0] === 'POST')[2].request_id;
  assert.match(document.getElementById('publication-state').textContent, new RegExp(id));
  assert.match(document.getElementById('publication-state').textContent, /status.*unavailable|could not.*refresh/i);
  assert.doesNotMatch(document.getElementById('publish').textContent, /retry/i);
});

test('HTTP transport preserves 409 status and sends revision-bound DELETE JSON', async () => {
  setup();
  const fetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    assert.equal(options.method, 'DELETE');
    assert.equal(options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(options.body), { expected_revision: 3 });
    return { status: 409, ok: false, json: async () => ({ error: 'changed elsewhere' }) };
  };
  try {
    await assert.rejects(transport.del('/api/playlists/p1', { expected_revision: 3 }), error => error.status === 409 && /changed elsewhere/.test(error.message));
  } finally { globalThis.fetch = fetch; }
});
