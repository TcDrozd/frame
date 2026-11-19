// Execute the production script, not a copy of its cache algorithm.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
// Node has no image decoder: use the existing Pi runtime dependency, Pillow,
// rather than a stub that accepts every nonempty body. This is NOT evidence of
// browser decoder behavior (Pillow rejects truncated JPEGs that Chrome's
// HTMLImageElement accepts); native decoding is covered in tests/browser-native.
const python = process.env.PYTHON || 'python3';
function pillow(code) {
  const result = spawnSync(python, ['-c', 'from PIL import Image; import io, sys, random\n' + code]);
  assert.equal(result.status, 0, result.stderr.toString());
  return result.stdout;
}
const validImage = pillow('Image.new("RGB", (3,2), "red").save(sys.stdout.buffer, "PNG")');
const noiseJpeg = pillow('r = random.Random(5); im = Image.frombytes("RGB", (64,48), bytes(r.randrange(256) for _ in range(64*48*3))); im.save(sys.stdout.buffer, "JPEG", quality=90)');
const tiffImage = pillow('b = io.BytesIO(); Image.new("RGB", (3,2), "red").save(b, "TIFF"); sys.stdout.buffer.write(b.getvalue())');
async function decode(blob) {
  const result = spawnSync(python, ['-c', 'from PIL import Image; import io,sys; b=sys.stdin.buffer.read(); Image.open(io.BytesIO(b), formats=["JPEG","PNG","WEBP","GIF"]).verify(); Image.open(io.BytesIO(b)).load()'],
    {input: Buffer.from(await blob.arrayBuffer())});
  if (result.status !== 0) throw Error('image decode failed');
  return {width:3, height:2, close() {}};
}
const source = fs.readFileSync(path.join(__dirname, '../apps/client/app.js'), 'utf8');
const KEYS = {photos: 'id', meta: 'key', staging: 'sid'};
function client(saved = {}) {
  const photos = saved.photos || new Map(), meta = saved.meta || new Map();
  const staging = saved.staging || new Map(), storage = saved.storage || new Map();
  const stores = {photos, meta, staging};
  const context = vm.createContext({ URL, URLSearchParams, AbortController, console, Blob, createImageBitmap: decode,
    window: {location: {href: 'https://frame.test/', search: ''}, addEventListener() {}},
    document: {readyState: 'loading', addEventListener() {}, getElementById: () => context.statusEl},
    localStorage: {getItem: k => storage.has(k) ? storage.get(k) : null, setItem: (k, v) => storage.set(k, String(v)),
      removeItem: k => storage.delete(k)},
    navigator: {},
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1,
    fetch: async (url) => {
      context.fetched.push(url);
      if (context.broken.has(url)) throw Error('offline');
      const bytes = context.bodies.get(url) || validImage;
      return {ok: true, headers: {get: name => name === "Content-Length" ? context.lengths.get(url) || null : null},
        blob: async () => new Blob([bytes], {type:'image/png'})};
    }, broken: new Set(), bodies: new Map(), lengths: new Map(), writes: [], fetched: [],
  });
  vm.runInContext(source, context);
  // Small asynchronous IDB fake with atomic commit/abort, shared on restart.
  const db = {transaction(names, mode) {
    const copies = {};
    for (const name of names) copies[name] = new Map(stores[name]);
    let puts = 0;
    const ops = []; // replayed on commit, like real IDB's serialized readwrite transactions
    const t = {error: null, abort() {t.error = Error('aborted'); setImmediate(() => t.onabort?.());},
      objectStore(name) {
        assert.ok(copies[name], 'store not in transaction scope: ' + name);
        const request = fn => {
          const req = {};
          setImmediate(() => {
            if (t.failed || t.error) return;
            try {req.result = fn(); req.onsuccess?.();}
            catch(e) {req.error=e; t.failed=true; t.error=context.hideTransactionError ? null : e; req.onerror?.(); t.onerror?.({target:req}); t.onabort?.();}
          });
          return req;
        };
        return {
          get: key => request(() => copies[name].get(key)),
          getAll: () => request(() => [...copies[name].values()]),
          getAllKeys: () => request(() => [...copies[name].keys()]),
          put: row => request(() => {
            if (context.stagingQuota && name === 'staging') {
              const e = Error('quota exceeded (staging)'); e.name = 'QuotaExceededError'; throw e;
            }
            if (context.quota && name !== 'staging' && ++puts === context.quota) {
              const e = Error('quota exceeded'); e.name = 'QuotaExceededError'; throw e;
            }
            copies[name].set(row[KEYS[name]], row);
            ops.push([name, 'set', row[KEYS[name]], row]);
            context.writes.push(name + ':' + row[KEYS[name]]);
          }),
          delete: key => request(() => {
            copies[name].delete(key); ops.push([name, 'delete', key]);
            context.writes.push('delete:' + name + ':' + key);
          }),
        };
      }};
    const finish = () => {
      if (!t.oncomplete && !t.onabort) return setImmediate(finish);
      if (t.failed || t.error) return;
      if (mode === 'readwrite') for (const [name, op, key, row] of ops) {
        if (op === 'set') stores[name].set(key, row); else stores[name].delete(key);
      }
      t.oncomplete?.();
    };
    // Allow queued requests and their promise continuations to run first.
    setImmediate(() => setImmediate(finish));
    return t;
  }};
  context.testDB = db;
  vm.runInContext('db = testDB; sleep = async () => {};', context);
  return {context, photos, meta, staging, storage, run: code => vm.runInContext(code, context)};
}
const raw = (photos, version='1') => ({schema: 2, version, mode:'sync', start_epoch:100, slide_seconds:10,
  photos: photos.map(([key, etag]) => ({id:key.split('/').pop(), key, etag, url:'https://frame.test/'+key+'?'+etag}))});
async function sync(c, manifest) {c.context.incoming = manifest; return c.run('syncCache(normalizeManifest(incoming))');}

test('partial replacement keeps prior manifest, signature and bytes; identical incoming retries', async () => {
  const c=client(); await sync(c,raw([['old/a.jpg','a']]));
  const before=c.meta.get('manifest').value, sig=c.meta.get('manifest_sig').value;
  const incoming=raw([['old/a.jpg','b'],['new/b.jpg','a']], '2');
  c.context.broken.add(incoming.photos[1].url);
  const failed=await sync(c,incoming);
  assert.equal(failed.changed,false);
  assert.deepEqual(c.meta.get('manifest').value,before);
  assert.equal(c.meta.get('manifest_sig').value,sig);
  assert.equal(c.photos.get('old/a.jpg').etag,'a');
  const restarted=client(c);
  const order=await restarted.run('(async () => buildPlayOrder(await metaGet("manifest")))()');
  assert.deepEqual(Array.from(order), ['old/a.jpg']);
  c.context.broken.clear();
  assert.equal((await sync(c,incoming)).changed,true);
  assert.equal(c.photos.get('old/a.jpg').etag,'b');
  assert.equal(c.photos.size,2);
});
test('total replacement failure retains the last good window', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  const incoming=raw([['new.jpg','b']], '2'); c.context.broken.add(incoming.photos[0].url);
  await sync(c,incoming);
  assert.equal(c.meta.get('manifest').value.version,'1');
  assert.equal(c.photos.has('old.jpg'),true);
});
test('play order retains holes rather than compacting sync indices', async () => {
  const c=client(); await sync(c,raw([['a.jpg','a'],['b.jpg','a'],['c.jpg','a']]));
  c.photos.delete('b.jpg');
  const ids=await c.run('buildPlayOrder(normalizeManifest(incoming))');
  assert.deepEqual(Array.from(ids), ['a.jpg',null,'c.jpg']);
});
test('active window is never trimmed to MAX_CACHED; schema 1 and full keys read', async () => {
  const c=client(); c.run('CONFIG.MAX_CACHED = 1');
  await sync(c,raw([['folder/a.jpg','a'],['other/a.jpg','a']]));
  assert.equal(c.photos.size,2);
  await sync(c,{schema:1,version:'legacy',photos:[{id:'legacy.jpg',url:'https://frame.test/legacy.jpg'}]});
  assert.equal(c.photos.has('legacy.jpg'),true);
});
test('quota failure atomically rolls back bytes, pruning and manifest promotion', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  const before=c.meta.get('manifest').value;
  c.context.writes=[];
  c.context.quota=3; // new bytes and manifest put succeed before signature put fails
  await assert.rejects(sync(c,raw([['new.jpg','b']], '2')), /quota/);
  assert.deepEqual(c.context.writes.filter(w => /^(photos|meta):/.test(w)), ['photos:new.jpg','meta:manifest']);
  assert.deepEqual(c.meta.get('manifest').value,before);
  assert.equal(c.photos.has('old.jpg'),true);
  assert.equal(c.photos.has('new.jpg'),false);
  c.context.quota=false;
  assert.equal((await sync(c,raw([['new.jpg','b']], '2'))).changed,true);
});
test('request error bubbles even before transaction.error is available; restart falls back', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  c.context.quota=3;
  c.context.hideTransactionError=true;
  await assert.rejects(sync(c,raw([['new.jpg','b']], '2')), /quota/);
  const restarted=client(c);
  assert.equal(restarted.meta.get('manifest').value.version,'1');
  await decode(restarted.photos.get('old.jpg').blob);
  assert.equal(restarted.photos.has('new.jpg'),false);
});
test('unchanged manifest repairs evicted bytes without losing order or epoch', async () => {
  const c=client(), m=raw([['a.jpg','a'],['b.jpg','a'],['c.jpg','a']]);
  await sync(c,m); c.photos.delete('b.jpg');
  assert.equal((await sync(c,m)).changed,true);
  assert.equal(c.meta.get('manifest').value.startEpoch,100);
  assert.deepEqual(Array.from(await c.run('(async () => buildPlayOrder(await metaGet("manifest")))()')),
    ['a.jpg','b.jpg','c.jpg']);
});
test('malformed/truncated bodies and rejecting decoder retain restart playback', async () => {
  for (const body of [Buffer.from('not image'), validImage.subarray(0,40)]) {
    const c=client(); await sync(c,raw([['old.jpg','a']]));
    const incoming=raw([['new.jpg','b']], '2');
    c.context.bodies.set(incoming.photos[0].url,body);
    assert.equal((await sync(c,incoming)).changed,false);
    const restarted=client(c);
    assert.equal(restarted.meta.get('manifest').value.version,'1');
    await decode(restarted.photos.get('old.jpg').blob);
    assert.equal(restarted.photos.has('new.jpg'),false);
  }
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  c.context.createImageBitmap=async () => {throw Error('explicit decoder rejection');};
  assert.equal((await sync(c,raw([['new.jpg','b']], '2'))).changed,false);
  assert.equal(c.meta.get('manifest').value.version,'1');
});
test('decodable but short Content-Length body is not promoted', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  const incoming=raw([['new.jpg','b']], '2');
  c.context.lengths.set(incoming.photos[0].url,String(validImage.length+100));
  assert.equal((await sync(c,incoming)).changed,false);
  assert.equal(c.photos.has('old.jpg'),true);
});
test('ETag introduced onto legacy cache refreshes bytes', async () => {
  const c=client(); await sync(c,raw([['a.jpg',undefined]]));
  await sync(c,raw([['a.jpg','new']]));
  assert.equal(c.photos.get('a.jpg').etag,'new');
});

// ---------- consumer review regressions ----------
const fetchedFor = (c, key) => c.context.fetched.filter(u => u.startsWith('https://frame.test/' + key + '?')).length;
test('undecodable entry becomes a hole; the rest of the window promotes with indices intact', async () => {
  for (const bad of [Buffer.from('not image'), tiffImage, noiseJpeg.subarray(0, Math.floor(noiseJpeg.length * 0.7))]) {
    const c=client(); await sync(c,raw([['old.jpg','a']]));
    const incoming=raw([['a.jpg','1'],['b.jpg','1'],['c.jpg','1']], '2');
    c.context.bodies.set(incoming.photos[1].url, bad);
    const res=await sync(c,incoming);
    assert.equal(res.changed,true);
    assert.equal(res.permanent,1);
    assert.equal(c.meta.get('manifest').value.version,'2');
    const restarted=client(c);
    assert.deepEqual(Array.from(await restarted.run('(async () => buildPlayOrder(await metaGet("manifest")))()')),
      ['a.jpg',null,'c.jpg']);
    assert.equal(c.photos.has('old.jpg'),false);
  }
});
test('transient failure alongside a hole keeps last good; all-hole window never promotes', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  const mixed=raw([['a.jpg','1'],['bad.jpg','1'],['net.jpg','1']], '2');
  c.context.bodies.set(mixed.photos[1].url, Buffer.from('x'));
  c.context.broken.add(mixed.photos[2].url);
  assert.equal((await sync(c,mixed)).changed,false);
  assert.equal(c.meta.get('manifest').value.version,'1');
  const allBad=raw([['bad1.jpg','1'],['bad2.jpg','1']], '3');
  for (const p of allBad.photos) c.context.bodies.set(p.url, Buffer.from('x'));
  const res=await sync(c,allBad);
  assert.equal(res.changed,false);
  assert.equal(res.fail,2);
  assert.equal(c.meta.get('manifest').value.version,'1');
  assert.equal(c.photos.has('old.jpg'),true);
});
test('permanent failures back off per (key, etag), persist across restart, cap at 24h', async () => {
  const c=client(); c.run('var clock = 1e12; Date.now = () => clock;');
  const m=raw([['a.jpg','1'],['bad.jpg','1']]);
  c.context.bodies.set(m.photos[1].url, tiffImage);
  await sync(c,m);
  assert.equal(fetchedFor(c,'bad.jpg'),1);
  const restarted=client(c); restarted.run('var clock = 1e12 + 60000; Date.now = () => clock;');
  restarted.context.bodies.set(m.photos[1].url, tiffImage);
  for (const version of ['1','1b']) await sync(restarted,{...m, version});
  assert.equal(fetchedFor(restarted,'bad.jpg'),0,'re-downloaded inside backoff');
  const record=Object.values(JSON.parse(restarted.storage.get(restarted.run('FAILURES_KEY'))))[0];
  restarted.run(`clock = ${record.retryAt + 1}`);
  await sync(restarted,m);
  assert.equal(fetchedFor(restarted,'bad.jpg'),1,'expired backoff must retry');
  for (let i=0;i<12;i++) {
    const rec=Object.values(JSON.parse(restarted.storage.get(restarted.run('FAILURES_KEY'))))[0];
    assert.ok(rec.retryAt - restarted.run('clock') <= 24*3600*1000);
    restarted.run(`clock = ${rec.retryAt + 1}`);
    await sync(restarted,m);
  }
});
test('changed etag clears the permanent-failure record and downloads', async () => {
  const c=client(); const m=raw([['a.jpg','1'],['bad.jpg','1']]);
  c.context.bodies.set(m.photos[1].url, Buffer.from('x'));
  await sync(c,m);
  const fixed=raw([['a.jpg','1'],['bad.jpg','2']], '2');
  assert.equal((await sync(c,fixed)).changed,true);
  assert.deepEqual(JSON.parse(c.storage.get(c.run('FAILURES_KEY'))), {});
  assert.equal(c.photos.get('bad.jpg').etag,'2');
});
test('downloads are written to IDB staging immediately and reused after a failed run', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  const incoming=raw([['a.jpg','1'],['b.jpg','1']], '2');
  c.context.broken.add(incoming.photos[1].url);
  assert.equal((await sync(c,incoming)).changed,false);
  assert.equal(c.staging.size,1,'successful download not staged in IDB');
  assert.equal(c.photos.has('a.jpg'),false,'unreferenced generation reached playback store');
  c.context.broken.clear(); c.context.fetched=[];
  assert.equal((await sync(c,incoming)).changed,true);
  assert.equal(fetchedFor(c,'a.jpg'),0,'staged generation was re-downloaded');
  assert.equal(c.staging.size,0);
  assert.deepEqual([...c.photos.keys()].sort(), ['a.jpg','b.jpg']);
  // Generations a newer manifest no longer wants are dropped (bounded staging).
  const next=raw([['x.jpg','1'],['y.jpg','1']], '3'); c.context.broken.add(next.photos[1].url);
  await sync(c,next);
  const after=raw([['z.jpg','1'],['w.jpg','1']], '4'); c.context.broken.add(after.photos[1].url);
  await sync(c,after);
  assert.deepEqual([...c.staging.keys()].map(k => k.split('\u0000')[0]), ['z.jpg']);
});
test('promotion issues deletes before puts', async () => {
  const c=client(); await sync(c,raw([['old1.jpg','a'],['old2.jpg','a']]));
  c.context.writes=[];
  await sync(c,raw([['new.jpg','b']], '2'));
  const w=c.context.writes.filter(x => !x.startsWith('staging:'));
  const lastDelete=Math.max(w.indexOf('delete:photos:old1.jpg'), w.indexOf('delete:photos:old2.jpg'));
  assert.ok(lastDelete >= 0 && lastDelete < w.indexOf('photos:new.jpg'), w.join(','));
});
test('migration: an existing v1 cache keeps playing and refreshes in place', async () => {
  const c=client();
  c.photos.set('legacy/a.jpg', {id:'legacy/a.jpg', blob:new Blob([validImage]), etag:'a', ts:1});
  c.meta.set('manifest', {key:'manifest', value: c.run('normalizeManifest')(raw([['legacy/a.jpg','a']]))});
  assert.deepEqual(Array.from(await c.run('(async () => buildPlayOrder(await metaGet("manifest")))()')), ['legacy/a.jpg']);
  assert.equal((await sync(c,raw([['legacy/a.jpg','a'],['b.jpg','a']], '2'))).changed,true);
  assert.equal(fetchedFor(c,'legacy/a.jpg'),0);
});
test('normalizeManifest rejects entries without identity and non-finite timing (matches Pi)', () => {
  const c=client();
  for (const bad of [{photos:[{url:'u'}]}, {photos:[null]}, {photos:[{id:'a'}], start_epoch:'soon'},
                     {photos:[{id:'a'}], slide_seconds: Infinity}, {photos:[{id:'a'}], start_epoch: true}]) {
    c.context.incoming=bad;
    assert.throws(() => c.run('normalizeManifest(incoming)'), /invalid manifest/, JSON.stringify(bad));
  }
  c.context.incoming={photos:[{key:'k/a.jpg'}], start_epoch: 5, slide_seconds: 10};
  const ok=c.run('normalizeManifest(incoming)');
  assert.equal(ok.photos[0].cid,'k/a.jpg'); assert.equal(ok.photos[0].id,'a.jpg');
});
test('truncated JPEG is rejected even by a decoder that accepts it; trailers after EOI are fine', async () => {
  const c=client();
  c.context.createImageBitmap=async () => ({width:1, height:1, close() {}}); // Chrome-like leniency
  for (const keep of [0.5, 0.7, 0.98]) {
    const truncated=new Blob([noiseJpeg.subarray(0, Math.floor(noiseJpeg.length*keep))]);
    c.context.blob=truncated;
    await assert.rejects(c.run('validateImageBlob(blob)'), /JPEG/, String(keep));
  }
  c.context.blob=new Blob([noiseJpeg, Buffer.from('SEFT trailer data \xff\xd8 after EOI')]);
  await c.run('validateImageBlob(blob)');
  c.context.blob=new Blob([noiseJpeg]);
  await c.run('validateImageBlob(blob)');
});
test('quota errors surface in the status overlay even after it auto-hid', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  c.context.statusEl={textContent:'', style:{display:'none'}};
  c.run('statusHidden = true');
  c.context.incoming=raw([['new.jpg','b']], '2');
  c.run('fetchManifest = async () => incoming; refreshPlayback = async () => {};');
  c.context.quota=2;
  await c.run('periodic()');
  assert.match(c.context.statusEl.textContent, /storage full/i);
  assert.notEqual(c.context.statusEl.style.display, 'none');
});
test('persistent storage is requested and estimated when available, tolerated when not', async () => {
  const c=client();
  assert.equal(await c.run('requestPersistentStorage()'), null);
  const calls=[];
  c.context.navigator.storage={persist: async () => {calls.push('persist'); return true;},
    estimate: async () => {calls.push('estimate'); return {usage: 10, quota: 100};}};
  const out=await c.run('requestPersistentStorage()');
  assert.deepEqual(calls, ['persist','estimate']);
  assert.equal(out.persisted, true);
});
test('status counts only slots that are actually cached', async () => {
  const c=client(); await sync(c,raw([['a.jpg','a'],['b.jpg','a']]));
  c.photos.delete('b.jpg');
  c.context.statusEl={textContent:'', style:{}};
  c.run('showById = async () => {}');
  await c.run('refreshPlayback()');
  assert.match(c.context.statusEl.textContent, /1\/2 cached/);
});
test('a hole over an older generation is a real hole, not stale content', async () => {
  const c=client(); await sync(c,raw([['a.jpg','1'],['b.jpg','1']]));
  const next=raw([['a.jpg','1'],['b.jpg','2']], '2');
  c.context.bodies.set(next.photos[1].url, Buffer.from('x'));
  assert.equal((await sync(c,next)).changed,true);
  assert.equal(c.photos.has('b.jpg'),false);
  assert.deepEqual(Array.from(await c.run('(async () => buildPlayOrder(await metaGet("manifest")))()')), ['a.jpg',null]);
});

// ---------- re-review regressions ----------
const playOrder = c => c.run('(async () => buildPlayOrder(await metaGet("manifest")))()');
function fakeIndexedDB(c, {onDiskVersion = 3, stores = ['photos','meta','staging'], blocked = false} = {}) {
  const opens = [], handles = [];
  c.context.indexedDB = {open(name, version) {
    opens.push([name, version]);
    const req = {};
    setImmediate(() => {
      if (blocked) req.onblocked?.();
      if (version !== undefined && version < onDiskVersion) {
        req.error = Object.assign(Error(`requested version (${version}) is less than existing (${onDiskVersion})`), {name: 'VersionError'});
        return req.onerror?.();
      }
      const handle = {transaction: c.context.testDB.transaction, version: onDiskVersion, closed: 0,
        objectStoreNames: {contains: n => stores.includes(n)}, close() { handle.closed++; }};
      handles.push(handle);
      req.result = handle; req.onsuccess?.();
    });
    return req;
  }};
  return {opens, handles};
}
test('I1: a newer on-disk DB version (rollback) reopens unversioned and keeps playing cached content', async () => {
  const c=client(); await sync(c,raw([['a.jpg','1']]));
  const {opens, handles} = fakeIndexedDB(c);
  const reloads=[]; c.context.window.location.reload = () => reloads.push(1);
  c.context.statusEl={textContent:'', style:{}};
  c.run('showById = async () => {}');
  await c.run('init()');
  assert.deepEqual(opens, [['SharedPhotoFrameDB', 2], ['SharedPhotoFrameDB', undefined]]);
  assert.doesNotMatch(c.context.statusEl.textContent, /fatal/);
  assert.deepEqual(Array.from(await playOrder(c)), ['a.jpg']);
  // A later tab/build upgrading the schema must not wedge this one.
  handles[0].onversionchange();
  assert.equal(handles[0].closed, 1);
  assert.equal(reloads.length, 1);
});
test('I1: unversioned reopen without the required stores fails loudly; blocked open is visible', async () => {
  const c=client();
  const {handles} = fakeIndexedDB(c, {stores: ['photos','meta']});
  await assert.rejects(c.run('openDB()'), /staging/);
  assert.equal(handles[0].closed, 1);
  const b=client(); b.context.statusEl={textContent:'', style:{}};
  fakeIndexedDB(b, {onDiskVersion: 2, blocked: true});
  const seen=[]; b.run('setStatus = (m) => seenStatus.push(m)'.replace('seenStatus', 'statusLog'));
  b.context.statusLog = seen;
  const d = await b.run('openDB()');
  assert.equal(d.version, 2);
  assert.ok(seen.some(m => /blocked/i.test(m)), seen.join(' | '));
  assert.equal(typeof d.onversionchange, 'function');
});
test('M1: staged rows of a withdrawn candidate are dropped when the publisher returns to the current manifest', async () => {
  const c=client(); const current=raw([['a.jpg','1']]); await sync(c,current);
  const candidate=raw([['x.jpg','1'],['y.jpg','1']], '2'); c.context.broken.add(candidate.photos[1].url);
  assert.equal((await sync(c,candidate)).changed,false);
  assert.equal(c.staging.size,1);
  assert.equal((await sync(c,current)).changed,false);
  assert.equal(c.staging.size,0,'withdrawn candidate rows survived the unchanged-manifest early return');
});
test('M3: a retryAt beyond the backoff cap (wrong clock) is treated as expired', async () => {
  const c=client(); c.run('var clock = 1e12; Date.now = () => clock;');
  const m=raw([['a.jpg','1'],['bad.jpg','1']]);
  c.context.bodies.set(m.photos[1].url, tiffImage);
  await sync(c,m);
  const key=c.run('FAILURES_KEY'), failures=JSON.parse(c.storage.get(key));
  for (const rec of Object.values(failures)) rec.retryAt = 1e12 + 30*24*3600*1000;
  c.storage.set(key, JSON.stringify(failures));
  c.context.bodies.delete(m.photos[1].url); c.context.fetched=[];
  assert.equal((await sync(c,{...m, version:'1b'})).changed,true);
  assert.equal(fetchedFor(c,'bad.jpg'),1,'hole pinned by a far-future retryAt');
  assert.deepEqual(Array.from(await playOrder(c)), ['a.jpg','bad.jpg']);
});
test('M4: a photo without a URL is a permanent hole, not a transient freeze; a fixed entry downloads at once', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  const m=raw([['a.jpg','1'],['nourl.jpg','1']], '2'); delete m.photos[1].url;
  const res=await sync(c,m);
  assert.equal(res.changed,true); assert.equal(res.transient,0); assert.equal(res.permanent,1);
  assert.deepEqual(Array.from(await playOrder(c)), ['a.jpg',null]);
  m.photos[1].url='https://frame.test/nourl.jpg?1'; m.version='3';
  assert.equal((await sync(c,m)).changed,true);
  assert.deepEqual(Array.from(await playOrder(c)), ['a.jpg','nourl.jpg']);
});
test('M4: schema-1 images[] (no URLs) promotes only over cached ids; nothing cached keeps last good', async () => {
  const c=client(); await sync(c,{schema:1,version:'s1',photos:[{id:'a.jpg',url:'https://frame.test/a.jpg'}]});
  const res=await sync(c,{schema:1,version:'s1b',images:['a.jpg','b.jpg']});
  assert.equal(res.changed,true); assert.equal(res.transient,0);
  assert.deepEqual(Array.from(await playOrder(c)), ['a.jpg',null]);
  const empty=client(); await sync(empty,raw([['old.jpg','a']]));
  const none=await sync(empty,{schema:1,version:'s1',images:['x.jpg','y.jpg']});
  assert.equal(none.changed,false); assert.equal(none.transient,0);
  assert.equal(empty.meta.get('manifest').value.version,'1');
});
function paddedJpeg(junk = Buffer.from([0x00, 0x00, 0x00])) {
  // Fill bytes between the APP0 segment and the next marker.
  assert.equal(noiseJpeg[2], 0xff); assert.equal(noiseJpeg[3], 0xe0);
  const end = 4 + noiseJpeg.readUInt16BE(4);
  return Buffer.concat([noiseJpeg.subarray(0, end), junk, noiseJpeg.subarray(end)]);
}
test('M7: padding between JPEG segments is accepted like Pillow; truncation still rejected', async () => {
  const padded=paddedJpeg();
  await decode(new Blob([padded])); // Pillow (the Pi) plays it
  const c=client(); c.context.createImageBitmap=async () => ({width:1, height:1, close() {}});
  c.context.blob=new Blob([padded]);
  await c.run('validateImageBlob(blob)');
  c.context.blob=new Blob([paddedJpeg(Buffer.alloc(5000, 0x41))]);
  await c.run('validateImageBlob(blob)');
  for (const keep of [0.5, 0.9]) {
    c.context.blob=new Blob([padded.subarray(0, Math.floor(padded.length*keep))]);
    await assert.rejects(c.run('validateImageBlob(blob)'), /JPEG/, String(keep));
  }
  c.context.blob=new Blob([Buffer.concat([padded.subarray(0, 40), Buffer.alloc(200, 0x11)])]);
  await assert.rejects(c.run('validateImageBlob(blob)'), /JPEG/);
});
test('M8: quota error on a staging write surfaces storage-full and preserves last good', async () => {
  const c=client(); await sync(c,raw([['old.jpg','a']]));
  const before=c.meta.get('manifest').value;
  c.context.statusEl={textContent:'', style:{display:'none'}};
  c.run('statusHidden = true');
  c.context.stagingQuota=true;
  await assert.rejects(sync(c,raw([['new.jpg','b']], '2')), /quota/);
  assert.match(c.context.statusEl.textContent, /storage full/i);
  assert.notEqual(c.context.statusEl.style.display, 'none');
  assert.deepEqual(c.meta.get('manifest').value, before);
  assert.equal(c.photos.has('old.jpg'),true);
  assert.equal(c.staging.size,0);
  c.context.stagingQuota=false;
  assert.equal((await sync(c,raw([['new.jpg','b']], '2'))).changed,true);
});
test('M-c: photo decode-validation is serialized even though downloads run concurrently', async () => {
  // A 50 MB photo decodes to hundreds of MB on Android; four at once (one per
  // download worker) is the difference between a cached window and an OOM tab.
  const c=client();
  let active=0, peak=0, decoded=0, fetching=0, peakFetch=0;
  const tick = () => new Promise(resolve => setImmediate(resolve));
  c.context.createImageBitmap=async () => {
    active++; peak=Math.max(peak, active);
    for (let i=0; i<5; i++) await tick();
    active--; decoded++;
    return {width:1, height:1, close() {}};
  };
  const fetchReal=c.context.fetch;
  c.context.fetch=async (url) => {
    fetching++; peakFetch=Math.max(peakFetch, fetching);
    await tick(); await tick();
    try { return await fetchReal(url); } finally { fetching--; }
  };
  const keys=['a','b','c','d','e','f'].map(k => [`w/${k}.jpg`, 'x']);
  const res=await sync(c,raw(keys));
  assert.equal(res.changed,true);
  assert.equal(decoded,6);
  assert.equal(peak,1,'createImageBitmap ran concurrently');
  assert.ok(peakFetch>1,'downloads should still overlap');
  // A rejected decode must not wedge the queue for later photos.
  const d=client(); let n=0;
  d.context.createImageBitmap=async () => { if (n++===0) throw Error('bad'); await tick(); return {width:1,height:1,close() {}}; };
  const r2=await sync(d,raw([['x/a.jpg','1'],['x/b.jpg','1'],['x/c.jpg','1']]));
  assert.equal(r2.changed,true); assert.equal(r2.permanent,1);
});
test('M-d: a decode that never settles times out as TRANSIENT; the queue proceeds and no hole is recorded', async () => {
  const c=client();
  const limit=c.run('CONFIG.DECODE_TIMEOUT_MS');
  // Only the decode timer fires (immediately); fetch timers stay inert as elsewhere.
  const armed=[];
  c.context.setTimeout=(fn, ms) => { if (ms===limit) { armed.push(ms); setImmediate(fn); } return 1; };
  let calls=0, decoded=0;
  c.context.createImageBitmap=async () => {
    if (calls++===0) return new Promise(() => {}); // hung decoder: never resolves or rejects
    decoded++; return {width:1, height:1, close() {}};
  };
  const keys=[['h/a.jpg','1'],['h/b.jpg','1'],['h/c.jpg','1']];
  let wedge;
  const guard=new Promise((_, reject) => { wedge=setTimeout(() => reject(Error('sync wedged on a hung decode')), 5000); });
  const res=await Promise.race([sync(c,raw(keys)), guard]);
  assert.ok(armed.length>=1,'decode timeout was never armed');
  assert.equal(decoded,2,'later photos must still be decoded');
  assert.equal(res.changed,false,'a transient failure keeps the last good manifest');
  assert.equal(res.transient,1); assert.equal(res.permanent,0);
  assert.deepEqual(JSON.parse(c.storage.get('frame_photo_failures') || '{}'),{},'a timeout is not a permanent hole');
  // Next poll (decoder healthy again) retries at once and promotes.
  c.context.createImageBitmap=async () => ({width:1, height:1, close() {}});
  const again=await Promise.race([sync(c,raw(keys)), guard]);
  clearTimeout(wedge);
  assert.equal(again.changed,true); assert.equal(again.permanent,0);
  assert.equal(typeof limit,'number'); assert.ok(limit>=10*1000 && limit<=120*1000, String(limit));
});
