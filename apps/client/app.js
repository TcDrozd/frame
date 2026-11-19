/**
 * Shared Photo Frame — Client v2 (Appliance-Grade)
 * - Pull manifest
 * - Cache photos in IndexedDB
 * - Render using two-layer crossfade
 * - Optional synchronized playback via manifest timing fields
 *
 * V2 additions:
 * - Better on-screen debug/status (works without DevTools)
 * - ?manifest= URL override (persisted to localStorage)
 * - Manifest signature change detection
 * - Early manifest refetch with backoff on expired photo URLs (403/404)
 * - Fetch timeouts; online/visibility-triggered resync
 * - Schema-2 aware: cache identity = key (falls back to id), etag-based
 *   re-download when content changes under the same key, url_expires_at
 *   proactive refetch
 * - One undecodable/oversized photo is a permanent per-(key, etag) failure:
 *   it becomes a hole (slot kept, previous frame held) with capped
 *   exponential backoff instead of freezing every future update.
 * - Downloads are written to an IndexedDB staging store as they arrive (not
 *   held in memory) and survive failed runs; promotion is one transaction.
 */

// ==================== CONFIG ====================
const CONFIG = {
  // Default manifest URL (fallback). You can override at runtime:
  //   http://<host>:8000/?manifest=https://.../manifest.json
  DEFAULT_MANIFEST_URL: "https://trevor-shared-photo-stream.s3.us-east-1.amazonaws.com/manifest.json",

  // How often to poll the manifest for updates (ms)
  MANIFEST_POLL_MS: 60 * 60 * 1000, // 1 hour

  // Transition fade ms (must match CSS transition)
  FADE_MS: 2000,

  // If manifest does NOT include timing fields, use this rotation time:
  DEFAULT_SLIDE_MS: 60 * 60 * 1000, // 1 hour

  // Show debug status overlay
  DISPLAY_STATUS: true,

  // Hide the overlay this long after the first photo renders. It stays up
  // if playback never starts, so boot failures remain visible on-screen.
  STATUS_HIDE_MS: 4000,

  // If true, append cache-bust to manifest fetches
  NO_CACHE_MANIFEST: true,

  // Fetch timeouts: a hung socket must not stall sync forever
  MANIFEST_TIMEOUT_MS: 15 * 1000,
  PHOTO_TIMEOUT_MS: 60 * 1000,
  // Decode-validation of one photo. A decoder promise that never settles
  // would otherwise wedge the serialized decode queue (and syncInFlight)
  // until reload; a timeout is TRANSIENT, so the photo is retried next sync.
  DECODE_TIMEOUT_MS: 30 * 1000,

  // Early-refetch backoff after expired photo URLs (403/404)
  EARLY_REFETCH_BACKOFF_MS: [60 * 1000, 5 * 60 * 1000, 30 * 60 * 1000],

  // Minimum gap between event-triggered syncs (online/visibility)
  NUDGE_MIN_GAP_MS: 30 * 1000,

  // Larger bodies are refused as permanent failures (holes), never retried hot.
  MAX_PHOTO_BYTES: 50 * 1024 * 1024,

  // Permanent per-photo failures retry on this schedule (doubling, capped).
  PERMANENT_BACKOFF_BASE_MS: 30 * 60 * 1000,
  PERMANENT_BACKOFF_MAX_MS: 24 * 60 * 60 * 1000,
};

// ==================== MANIFEST URL MANAGEMENT ====================
// Allows remote reconfiguration without editing app.js.
// Usage:
//   http://frame.local/?manifest=https://your-host/manifest.json
function getManifestUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = params.get("manifest");
    if (fromQuery) {
      localStorage.setItem("manifest_url", fromQuery);
      return fromQuery;
    }
    const stored = localStorage.getItem("manifest_url");
    return stored || CONFIG.DEFAULT_MANIFEST_URL;
  } catch {
    return CONFIG.DEFAULT_MANIFEST_URL;
  }
}
const MANIFEST_URL = getManifestUrl();

// ==================== STATUS ====================
let statusHidden = false;
let statusHideTimer = null;
let statusAlert = false; // a storage alert keeps the overlay up until a clean sync

function setStatus(msg) {
  try {
    if (!CONFIG.DISPLAY_STATUS || statusHidden) return;
    const el = document.getElementById("status");
    if (el) el.textContent = msg;
    // Mirror in URL hash so you can see it without DevTools / share screenshot context.
    window.location.hash = encodeURIComponent(String(msg)).slice(0, 180);
  } catch {}
}

// Problems that need a human (storage full) must be visible on-screen even
// after the overlay auto-hid, not only recorded in meta.last_error.
function showAlert(msg) {
  statusAlert = true;
  statusHidden = false;
  if (statusHideTimer) clearTimeout(statusHideTimer);
  statusHideTimer = null;
  try {
    const el = document.getElementById("status");
    if (el) el.style.display = "";
  } catch {}
  setStatus(msg);
}

function isQuotaError(e) {
  return Boolean(e && (e.name === "QuotaExceededError" || e.code === 22));
}

function hideStatusAfterFirstPhoto() {
  if (statusHidden || statusHideTimer || statusAlert) return;
  statusHideTimer = setTimeout(() => {
    statusHidden = true;
    const el = document.getElementById("status");
    if (el) el.style.display = "none";
  }, CONFIG.STATUS_HIDE_MS);
}

// Global error traps so "booting…" doesn't hide fatal issues on iPad
window.addEventListener("error", (e) => {
  const msg = e && e.message ? e.message : "unknown";
  setStatus("JS error: " + msg);
});
window.addEventListener("unhandledrejection", (e) => {
  const msg = e && e.reason && e.reason.message ? e.reason.message : String(e && e.reason ? e.reason : "unknown");
  setStatus("Promise error: " + msg);
});

// ==================== IDB (PROMISE WRAPPER) ====================
const DB_NAME = "SharedPhotoFrameDB";
// v2 adds `staging` (validated downloads not yet referenced by the active
// manifest). v1 photos/meta rows are untouched by the upgrade, so an existing
// cache keeps playing and later syncs reuse it.
// ROLLBACK RULE: never ship a build with DB_VER below 2. The released v1
// code throws VersionError on a v2 database and cannot be fixed; this code
// tolerates any NEWER on-disk version (see openDB) as long as its stores exist.
const DB_VER = 2;
const STORES = {
  photos: "photos", // key: id (= cid); only rows referenced by the promoted manifest
  meta: "meta", // key: key
  staging: "staging", // key: sid (= cid + "\u0000" + etag); never read by playback
};

let db;

function idbReq(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function openDBRequest(version) {
  return new Promise((resolve, reject) => {
    // No version = open whatever is on disk (never upgrades an existing DB).
    const req = version === undefined ? indexedDB.open(DB_NAME) : indexedDB.open(DB_NAME, version);

    // Another tab/old build holds the DB open: say so instead of hanging silently.
    req.onblocked = () => setStatus("init: database upgrade blocked by another open frame tab…");

    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains(STORES.photos)) {
        const s = d.createObjectStore(STORES.photos, { keyPath: "id" });
        s.createIndex("ts", "ts", { unique: false });
      }
      if (!d.objectStoreNames.contains(STORES.meta)) {
        d.createObjectStore(STORES.meta, { keyPath: "key" });
      }
      if (!d.objectStoreNames.contains(STORES.staging)) {
        d.createObjectStore(STORES.staging, { keyPath: "sid" });
      }
    };

    req.onsuccess = () => {
      const d = req.result;
      // A newer build upgrading the schema must not be blocked by (or wedge)
      // this page: release the handle and reload into the new code.
      d.onversionchange = () => {
        try { d.close(); } catch {}
        try { window.location.reload(); } catch {}
      };
      resolve(d);
    };
    req.onerror = () => reject(req.error);
  });
}

async function openDB() {
  try {
    return await openDBRequest(DB_VER);
  } catch (e) {
    if (!e || e.name !== "VersionError") throw e;
    // The on-disk DB is NEWER than this build (e.g. a rolled-back app.js).
    // Open it at its own version and keep playing if the stores we use exist.
    const d = await openDBRequest(undefined);
    const missing = Object.values(STORES).filter((name) => !d.objectStoreNames.contains(name));
    if (missing.length) {
      try { d.close(); } catch {}
      throw new Error(`database v${d.version} lacks stores: ${missing.join(", ")}`);
    }
    setStatus(`init: db v${d.version} is newer than client v${DB_VER}; continuing`);
    return d;
  }
}

async function tx(storeName, mode, fn) {
  const t = db.transaction([storeName], mode);
  // Register before issuing requests: request failure can abort the transaction
  // before an awaited callback resumes. Always consume both failure paths.
  const done = new Promise((resolve, reject) => {
    t.oncomplete = () => resolve();
    t.onerror = (event) => reject(t.error || event?.target?.error || new Error("cache transaction failed"));
    t.onabort = () => reject(t.error || new Error("cache transaction aborted"));
  });
  done.catch(() => {});
  try {
    const out = await fn(t.objectStore(storeName));
    await done;
    return out;
  } catch (error) {
    try { t.abort(); } catch {}
    await done.catch(() => {});
    throw error;
  }
}

async function metaGet(key) {
  return tx(STORES.meta, "readonly", async (s) => {
    const row = await idbReq(s.get(key));
    return row ? row.value : undefined;
  });
}

async function metaSet(key, value) {
  return tx(STORES.meta, "readwrite", async (s) => {
    await idbReq(s.put({ key, value }));
  });
}

async function photoPut({ id, blob, mime, name, sha256, etag, ts }) {
  return tx(STORES.photos, "readwrite", async (s) => {
    await idbReq(s.put({ id, blob, mime, name, sha256, etag, ts }));
  });
}

async function photoGet(id) {
  return tx(STORES.photos, "readonly", async (s) => {
    return idbReq(s.get(id));
  });
}

async function photoGetAll() {
  return tx(STORES.photos, "readonly", async (s) => {
    return idbReq(s.getAll());
  });
}

// Staged generations: each validated download is written in its own small
// transaction as soon as it arrives, so no replacement set is held in memory
// and a failed run keeps what it already fetched. Playback never reads here.
function stagingId(p) {
  return `${p.cid}\u0000${p.etag || ""}`;
}

async function stagingKeys() {
  return tx(STORES.staging, "readonly", async (s) => idbReq(s.getAllKeys()));
}

async function stagingPut(row) {
  return tx(STORES.staging, "readwrite", async (s) => {
    await idbReq(s.put(row));
  });
}

async function stagingDelete(sids) {
  if (!sids.length) return;
  return tx(STORES.staging, "readwrite", async (s) => {
    await Promise.all(sids.map((sid) => idbReq(s.delete(sid))));
  });
}

async function stagingGetMany(sids) {
  if (!sids.length) return [];
  return tx(STORES.staging, "readonly", async (s) => Promise.all(sids.map((sid) => idbReq(s.get(sid)))));
}

// ==================== PERMANENT PHOTO FAILURES ====================
// Undecodable/unsupported/oversized bytes for one (key, etag) will not fix
// themselves by retrying every poll. Records live in localStorage (persist
// across restart, outside the IDB stores playback reads); a changed ETag is a
// new identity, and records for photos no longer in the manifest are dropped.
const FAILURES_KEY = "frame_photo_failures";

class PermanentPhotoError extends Error {
  constructor(message) {
    super(message);
    this.name = "PermanentPhotoError";
  }
}

function failureId(p) {
  return `${p.cid}\u0000${p.etag || ""}`;
}

function loadFailures() {
  try {
    const value = JSON.parse(localStorage.getItem(FAILURES_KEY) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

function saveFailures(failures) {
  try {
    localStorage.setItem(FAILURES_KEY, JSON.stringify(failures));
  } catch {}
}

function recordFailure(failures, p, reason, now) {
  const id = failureId(p);
  const attempts = ((failures[id] && failures[id].attempts) || 0) + 1;
  const delay = Math.min(CONFIG.PERMANENT_BACKOFF_BASE_MS * 2 ** (attempts - 1), CONFIG.PERMANENT_BACKOFF_MAX_MS);
  failures[id] = { attempts, retryAt: now + delay, reason: String(reason).slice(0, 200) };
  if (!p.url) failures[id].noUrl = true;
}

// Inside its backoff window. A retryAt further away than the cap was written
// under a wrong clock and is treated as expired (it must not pin a hole), and
// a URL-less record no longer applies once the publisher supplies a URL.
function failurePending(record, p, now) {
  if (!record) return false;
  if (record.noUrl && p.url) return false;
  const remaining = record.retryAt - now;
  return remaining > 0 && remaining <= CONFIG.PERMANENT_BACKOFF_MAX_MS;
}

// Stable signature so we can detect changes even if `version` is unchanged.
function manifestSig(m) {
  const slim = {
    version: m.version,
    startEpoch: m.startEpoch,
    slideSeconds: m.slideSeconds,
    mode: m.mode,
    photos: (m.photos || []).map((p) => ({ id: p.cid || p.id, url: p.url, etag: p.etag, sha256: p.sha256 })),
  };
  return JSON.stringify(slim);
}

// ==================== MANIFEST ====================
async function fetchWithTimeout(url, opts, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...opts, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchManifest() {
  const base = new URL(window.location.href);
  const manifestUrl = new URL(MANIFEST_URL, base);

  const url = CONFIG.NO_CACHE_MANIFEST
    ? `${manifestUrl.toString()}?t=${Date.now()}`
    : manifestUrl.toString();

  const res = await fetchWithTimeout(url, { cache: "no-store" }, CONFIG.MANIFEST_TIMEOUT_MS);
  if (!res.ok) throw new Error(`manifest fetch failed: ${res.status}`);
  return res.json();
}

// Same acceptance rule as the Pi's frame_cache.valid_manifest: a malformed
// manifest is rejected as a whole (never compacted), so lockstep indices stay
// identical across clients.
function invalidManifest(reason) {
  return new Error(`invalid manifest: ${reason}`);
}

function finiteTiming(raw, field) {
  const value = raw[field];
  if (value == null) return undefined;
  if (typeof value !== "number" || !Number.isFinite(value)) throw invalidManifest(`${field} must be a finite number`);
  return value;
}

function normalizeManifest(raw) {
  if (!raw || typeof raw !== "object") throw invalidManifest("not an object");
  const version = String(raw.version ?? "0");

  const photos = Array.isArray(raw.photos)
    ? raw.photos
    : Array.isArray(raw.images)
      ? raw.images.map((id) => ({ id }))
      : [];

  const normPhotos = photos.map((p, index) => {
    if (!p || typeof p !== "object") throw invalidManifest(`photo ${index} is not an object`);
    const key = String(p.key ?? "").trim();
    const id = String(p.id ?? "").trim();
    // Cache identity: schema-2 full key when present (basenames collide
    // across folders), else the schema-1 id.
    const cid = key || id;
    if (!cid) throw invalidManifest(`photo ${index} has neither key nor id`);
    return {
    id: id || cid.split("/").pop(),
    cid,
    url: p.url ? String(p.url) : undefined,
    name: p.name ? String(p.name) : undefined,
    sha256: p.sha256 ? String(p.sha256) : undefined,
    etag: p.etag ? String(p.etag) : undefined,
    bytes: Number.isFinite(p.bytes) ? p.bytes : undefined,
    };
  });

  const startEpoch = finiteTiming(raw, "start_epoch");
  const slideSeconds = finiteTiming(raw, "slide_seconds");
  const mode = raw.mode ? String(raw.mode) : undefined;
  const urlExpiresAt = raw.url_expires_at != null ? Number(raw.url_expires_at) : undefined;

  return { version, photos: normPhotos, startEpoch, slideSeconds, mode, urlExpiresAt };
}

// ==================== DOWNLOAD ====================
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// JPEG structural completeness: walk the marker segments from SOI, skip each
// scan's entropy-coded data (FF00 stuffing / RSTn), and require an EOI (FFD9)
// marker. Bytes after EOI (e.g. phone trailers) are allowed; a body cut short
// anywhere before EOI — including mid-scan — is rejected. An EXIF thumbnail's
// own EOI cannot satisfy this: APPn segments are skipped by their length.
function jpegComplete(b) {
  const n = b.length;
  if (n < 4 || b[0] !== 0xff || b[1] !== 0xd8) return false;
  let i = 2;
  while (i < n) {
    // Junk/padding between segments is skipped exactly as Pillow (the Pi)
    // does, so both clients agree on what plays. i only moves forward.
    while (i < n && b[i] !== 0xff) i++;
    while (i + 1 < n && b[i + 1] === 0xff) i++; // fill bytes
    if (i + 1 >= n) return false;
    const marker = b[i + 1];
    i += 2;
    if (marker === 0xd9) return true;
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue; // standalone
    if (i + 1 >= n) return false;
    const length = (b[i] << 8) | b[i + 1];
    if (length < 2) return false;
    i += length;
    if (marker === 0xda) {
      while (i < n) {
        if (b[i] !== 0xff) { i++; continue; }
        if (i + 1 >= n) return false;
        const next = b[i + 1];
        if (next === 0x00 || (next >= 0xd0 && next <= 0xd7)) { i += 2; continue; }
        if (next === 0xff) { i++; continue; }
        break; // a real marker ends the scan
      }
    }
  }
  return false;
}

async function validateImageBlob(blob) {
  if (!blob || !blob.size) throw new Error("empty photo");
  const head = new Uint8Array(await blob.slice(0, 3).arrayBuffer());
  if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff &&
      !jpegComplete(new Uint8Array(await blob.arrayBuffer()))) {
    // Real Chrome decodes truncated JPEGs "successfully" (grey fill) through
    // HTMLImageElement, so decoder success alone is not proof of complete bytes.
    throw new Error("truncated JPEG (no end-of-image marker)");
  }
  if (typeof createImageBitmap === "function") {
    const image = await createImageBitmap(blob);
    try {
      if (!image.width || !image.height) throw new Error("invalid image dimensions");
    } finally {
      image.close();
    }
    return;
  }
  // Older browsers: the same decoder as playback. Note Chrome fires onload
  // for truncated baseline AND progressive JPEGs, so onload is not proof of
  // integrity: also await decode() and rely on the JPEG EOI check above.
  // Always release the temporary URL.
  const url = URL.createObjectURL(blob);
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = () => image.naturalWidth && image.naturalHeight
        ? resolve() : reject(new Error("invalid image dimensions"));
      image.onerror = () => reject(new Error("photo decode failed"));
      image.src = url;
    });
    if (typeof image.decode === "function") await image.decode();
  } finally {
    URL.revokeObjectURL(url);
  }
}

// Decode-validation runs one photo at a time. Photos may be up to
// MAX_PHOTO_BYTES (50 MB); createImageBitmap allocates the full decoded
// bitmap (a 48 MP photo is ~190 MB), plus the JPEG completeness check reads
// the whole body into an ArrayBuffer. With one decode per download worker,
// four at once could need ~768 MB on an Android tablet and kill the tab.
// Downloads stay concurrent; only this step is queued. A rejection never
// blocks the queue for later photos.
let decodeChain = Promise.resolve();
function serializedDecode(task) {
  const run = decodeChain.then(task, task);
  decodeChain = run.then(() => {}, () => {});
  return run;
}

// Not a PermanentPhotoError: a hung decoder says nothing about the bytes
// (GPU/process stall, tab throttling), so the photo is a transient failure
// that keeps the last good manifest and retries on the next sync.
class DecodeTimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = "DecodeTimeoutError";
  }
}

// Settles within DECODE_TIMEOUT_MS whatever the decoder does, so the
// serialized queue always advances. A timed-out decode may still finish in
// the background; its result is ignored.
function withDecodeTimeout(task) {
  return () => {
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new DecodeTimeoutError(
        `photo decode timed out after ${CONFIG.DECODE_TIMEOUT_MS} ms`)), CONFIG.DECODE_TIMEOUT_MS);
    });
    return Promise.race([Promise.resolve().then(task), timeout])
      .finally(() => clearTimeout(timer));
  };
}

async function downloadToCache(entry) {
  // A manifest property, not a network condition: retrying cannot fix it, so
  // it is a hole (failurePending lets a fixed entry download immediately).
  if (!entry.url) throw new PermanentPhotoError(`photo has no URL: ${entry.id}`);

  const base = new URL(window.location.href);
  const photoUrl = new URL(entry.url, base).toString();

  const res = await fetchWithTimeout(photoUrl, {}, CONFIG.PHOTO_TIMEOUT_MS);

  if (!res.ok) {
    // Photo URL expired or object missing: refetch the manifest early
    // (with backoff) instead of waiting out the hourly poll.
    if (res.status === 403 || res.status === 404) {
      scheduleEarlyRefetch(`url dead (${res.status})`);
    }
    throw new Error(`download failed ${entry.id}: ${res.status}`);
  }

  const length = res.headers.get("Content-Length");
  if (length !== null && /^\d+$/.test(length) && Number(length) > CONFIG.MAX_PHOTO_BYTES) {
    throw new PermanentPhotoError(`photo exceeds size cap: ${entry.id}`);
  }
  const blob = await res.blob();
  const mime = blob.type || "application/octet-stream";

  // Fetch normally rejects short transfers; check explicit lengths as well.
  // Content-Encoding lengths describe compressed transport, not Blob bytes.
  // A short body is TRANSIENT (retry), unlike undecodable complete bytes.
  if (!res.headers.get("Content-Encoding") && length !== null &&
      (!/^\d+$/.test(length) || Number(length) !== blob.size)) {
    throw new Error(`incomplete photo: ${entry.id}`);
  }
  if (blob.size > CONFIG.MAX_PHOTO_BYTES) throw new PermanentPhotoError(`photo exceeds size cap: ${entry.id}`);
  try {
    await serializedDecode(withDecodeTimeout(() => validateImageBlob(blob)));
  } catch (e) {
    if (e && e.name === "DecodeTimeoutError") throw e; // transient: retried next sync
    throw new PermanentPhotoError(`photo undecodable: ${entry.id}: ${e && e.message ? e.message : e}`);
  }
  // Staged in IDB by the caller; never written over bytes the active
  // manifest references until the promotion transaction commits.
  return {
    sid: stagingId(entry), id: entry.cid, blob, mime, name: entry.name,
    sha256: entry.sha256, etag: entry.etag, ts: Date.now(),
  };
}

async function syncCache(manifest) {
  setStatus("sync: checking…");

  const cached = await metaGet("manifest");
  const cachedVer = cached?.version;

  const newSig = manifestSig(manifest);
  const cachedSig = await metaGet("manifest_sig");

  const cachedPhotos = await photoGetAll();
  const cachedById = new Map(cachedPhotos.map((p) => [p.id, p]));
  const now = Date.now();
  const failures = loadFailures();
  const holePending = (p) => failurePending(failures[failureId(p)], p, now);
  const stale = (p) => {
    const row = cachedById.get(p.cid);
    return !row?.blob || Boolean(p.etag && p.etag !== row.etag) ||
      Boolean(p.sha256 && p.sha256 !== row.sha256);
  };
  const needsDownload = (p) => stale(p) && !holePending(p);

  // Staging holds only generations this manifest still wants (bounded);
  // anything already staged by an earlier failed run is reused, not refetched.
  // Runs before the early returns below: a candidate the publisher withdrew
  // (back to the current manifest) must not leave its rows behind.
  const wantedSids = new Set(manifest.photos.filter(stale).map(stagingId));
  const stagedKeys = await stagingKeys();
  await stagingDelete(stagedKeys.filter((sid) => !wantedSids.has(sid)));
  const staged = new Set(stagedKeys.filter((sid) => wantedSids.has(sid)));

  // Even an unchanged manifest must repair missing/evicted cache entries
  // (permanent holes wait for their backoff instead of retrying every poll).
  if (cachedVer === manifest.version && cachedSig === newSig &&
      !manifest.photos.some(needsDownload)) {
    await metaSet("last_check", Date.now());
    return { changed: false };
  }
  // An empty/malformed replacement cannot destroy working playback.
  if (!manifest.photos.length) return { changed: false, fail: 1, transient: 1 };
  const manifestIds = new Set(manifest.photos.map((p) => p.cid));
  const toDownload = manifest.photos.filter((p) => needsDownload(p) && !staged.has(stagingId(p)));

  let ok = staged.size;
  let fail = 0;
  let permanent = 0;
  let quotaError = null;
  const CONCURRENCY = 4;
  const queue = [...toDownload];

  async function worker() {
    while (queue.length) {
      const item = queue.shift();
      try {
        setStatus(`sync: dl ${ok + fail + permanent + 1}/${toDownload.length + staged.size}…`);
        const row = await downloadToCache(item);
        await stagingPut(row); // persisted now; dropped from memory
        staged.add(row.sid);
        delete failures[failureId(item)];
        ok++;
      } catch (e) {
        if (e && e.name === "PermanentPhotoError") {
          permanent++;
          recordFailure(failures, item, e.message, now);
          setStatus(`sync: unplayable photo skipped (${permanent})…`);
        } else {
          fail++;
          if (isQuotaError(e)) quotaError = e;
          setStatus(`sync: dl failed (${fail})…`);
          await sleep(500);
        }
      }
    }
  }

  const workers = Array.from({ length: Math.min(CONCURRENCY, toDownload.length) }, () => worker());
  await Promise.all(workers);

  // A changed ETag (or a photo leaving the window) clears its record.
  const wantedFailures = new Set(manifest.photos.map(failureId));
  for (const id of Object.keys(failures)) if (!wantedFailures.has(id)) delete failures[id];
  saveFailures(failures);

  if (quotaError) {
    showAlert(`storage full: keeping last good manifest (${quotaError.message || quotaError.name})`);
    throw quotaError;
  }

  // A slot is playable if its CURRENT generation is cached or staged;
  // anything else is a hole. Holes keep their slot so lockstep indices never
  // shift (playback holds the previous frame), matching the Pi client. Never
  // promote a window that cannot show anything: that would replace a
  // playable show with a frozen frame.
  const isPlayable = (p) => !stale(p) || staged.has(stagingId(p));
  const playable = manifest.photos.filter(isPlayable).length;
  if (fail || !playable) {
    if (fail) scheduleEarlyRefetch("incomplete replacement");
    setStatus(`sync: keeping last good manifest (${fail + permanent} failed)`);
    return { changed: false, ok, fail: fail + permanent, transient: fail, permanent, deleted: 0 };
  }

  const promoteSids = manifest.photos.map(stagingId).filter((sid, i, all) => staged.has(sid) && all.indexOf(sid) === i);
  const promoteRows = (await stagingGetMany(promoteSids)).filter((row) => row && row.blob);
  if (promoteRows.length !== promoteSids.length) throw new Error("staged photo vanished before promotion");

  // One IDB transaction promotes bytes + manifest/signature and prunes. A
  // quota error/abort leaves the ENTIRE prior window intact, even on reboot
  // (staged rows also survive for the retry). Deletes are issued before puts
  // to keep peak storage low. Photos referenced by the manifest are never
  // pruned; there is no separate cache-size cap.
  // Also prune an older generation sitting in a hole's slot, so the hole is a
  // real hole rather than stale content under the new manifest's identity.
  const holeIds = new Set(manifest.photos.filter((p) => !isPlayable(p)).map((p) => p.cid));
  const keepIds = new Set([...manifestIds].filter((id) => !holeIds.has(id)));
  const toDelete = cachedPhotos.filter((p) => !keepIds.has(p.id));
  await new Promise((resolve, reject) => {
    const t = db.transaction([STORES.photos, STORES.meta, STORES.staging], "readwrite");
    t.oncomplete = () => resolve();
    t.onerror = (event) => reject(t.error || event?.target?.error || new Error("cache promotion failed"));
    t.onabort = () => reject(t.error || new Error("cache promotion aborted"));
    try {
      const photos = t.objectStore(STORES.photos);
      for (const row of toDelete) photos.delete(row.id);
      const staging = t.objectStore(STORES.staging);
      for (const { sid, ...row } of promoteRows) {
        photos.put(row);
        staging.delete(sid);
      }
      const meta = t.objectStore(STORES.meta);
      meta.put({ key: "manifest", value: manifest });
      meta.put({ key: "manifest_sig", value: newSig });
      meta.put({ key: "last_sync", value: Date.now() });
      meta.put({ key: "last_check", value: Date.now() });
    } catch (e) {
      t.abort();
      reject(e);
    }
  });

  statusAlert = false;
  setStatus(`sync: done v${manifest.version} (+${promoteRows.length}/-${toDelete.length}, holes ${manifest.photos.length - playable})`);
  return { changed: true, ok, fail: permanent, transient: 0, permanent, deleted: toDelete.length };
}

// ==================== PLAYBACK LOGIC ====================
function getSlideMs(manifest) {
  if (manifest.slideSeconds && manifest.slideSeconds > 0) return manifest.slideSeconds * 1000;
  return CONFIG.DEFAULT_SLIDE_MS;
}

function inSyncMode(manifest) {
  return (
    manifest.mode === "sync" &&
    Number.isFinite(manifest.startEpoch) &&
    Number.isFinite(manifest.slideSeconds) &&
    manifest.photos.length > 0
  );
}

function calcIndex(manifest) {
  const n = manifest.photos.length;
  if (n === 0) return 0;

  const slideMs = getSlideMs(manifest);
  const now = Date.now();

  if (inSyncMode(manifest)) {
    const startMs = manifest.startEpoch * 1000;
    const elapsed = now - startMs;
    if (elapsed < 0) {
      const fallbackSteps = Math.floor(now / slideMs);
      return fallbackSteps % n;
    }
    const steps = Math.floor(elapsed / slideMs);
    return steps % n;
  }

  const steps = Math.floor(now / slideMs);
  return steps % n;
}

function msUntilNext(manifest) {
  const slideMs = getSlideMs(manifest);
  const now = Date.now();

  // Sync mode: slide boundaries are phased from start_epoch, matching
  // calcIndex — otherwise frames wake at absolute-clock boundaries and
  // drift within a slide whenever start_epoch isn't slide-aligned.
  if (inSyncMode(manifest)) {
    const startMs = manifest.startEpoch * 1000;
    if (now >= startMs) {
      const elapsed = now - startMs;
      return slideMs - (elapsed % slideMs);
    }
  }

  const next = Math.ceil(now / slideMs) * slideMs;
  return next - now;
}

// ==================== RENDERER (2-LAYER CROSSFADE) ====================
const layerA = () => document.querySelector("#layerA");
const layerB = () => document.querySelector("#layerB");

let activeLayer = "A";
let currentId = null;
let timer = null;

function swapLayer() {
  activeLayer = activeLayer === "A" ? "B" : "A";
  return activeLayer === "A" ? layerA() : layerB();
}

function getInactiveLayer() {
  return activeLayer === "A" ? layerB() : layerA();
}

function setLayerActive(layerEl, on) {
  layerEl.classList.toggle("active", !!on);
}

function setImg(layerEl, url) {
  const img = layerEl.querySelector("img");
  img.src = url;
}

function revokeImg(layerEl) {
  const img = layerEl.querySelector("img");
  const src = img.src;
  if (src && src.startsWith("blob:")) URL.revokeObjectURL(src);
  img.src = "";
}

async function showById(id) {
  if (!id || id === currentId) return;

  const row = await photoGet(id);
  if (!row?.blob) return;

  const nextLayer = swapLayer();
  const prevLayer = getInactiveLayer();

  const url = URL.createObjectURL(row.blob);
  const img = nextLayer.querySelector("img");
  setImg(nextLayer, url);

  // Decode before flipping visible layer to reduce "hiccup"
  if (img.decode) {
    try {
      await img.decode();
    } catch {
      // ignore decode errors
    }
    setLayerActive(nextLayer, true);
  } else {
    await new Promise((resolve) => {
      img.onload = () => {
        setLayerActive(nextLayer, true);
        img.onload = null;
        resolve();
      };
    });
  }

  setTimeout(() => {
    setLayerActive(prevLayer, false);
    setTimeout(() => revokeImg(prevLayer), CONFIG.FADE_MS + 50);
  }, 50);

  currentId = id;
  hideStatusAfterFirstPhoto();
}

// ==================== LOOP ====================
async function buildPlayOrder(manifest) {
  const cached = await photoGetAll();
  const map = new Map(cached.map((p) => [p.id, p]));
  // Keep one slot per manifest entry: compacting holes shifts every later
  // synchronized slide away from the publisher's clock/order.
  return manifest.photos.map((p) => {
    const row = map.get(p.cid || p.id);
    return row?.blob ? row.id : null;
  });
}

let playIds = [];
let manifestMem = null;

async function tick() {
  if (!manifestMem || playIds.length === 0) return;

  const idx = calcIndex(manifestMem);
  const id = playIds[idx];
  await showById(id);

  const wait = msUntilNext(manifestMem);
  if (timer) clearTimeout(timer);
  timer = setTimeout(tick, wait);
}

async function refreshPlayback() {
  const m = await metaGet("manifest");
  if (!m) {
    setStatus("playback: no manifest cached yet");
    return;
  }

  manifestMem = m;
  playIds = await buildPlayOrder(m);
  const cachedCount = playIds.filter(Boolean).length;

  if (cachedCount === 0) {
    setStatus("playback: no cached photos yet");
    return;
  }

  await tick();
  setStatus(`playback: ${inSyncMode(m) ? "sync" : "mvp"} (${cachedCount}/${playIds.length} cached, v${m.version})`);
}

// ==================== PERIODIC SYNC ====================
let syncInFlight = false;
let lastPeriodicAt = 0;
let earlyRefetchTimer = null;
let earlyRefetchIdx = 0;

// Schedule a one-off early manifest refetch (used when photo URLs turn out
// to be dead, and near url_expires_at). Backoff prevents a stale manifest
// with permanently dead URLs from turning the hourly poll into a hot loop.
function scheduleEarlyRefetch(reason, delayMs) {
  if (earlyRefetchTimer) return;
  const steps = CONFIG.EARLY_REFETCH_BACKOFF_MS;
  const delay = delayMs ?? steps[Math.min(earlyRefetchIdx, steps.length - 1)];
  if (delayMs == null) earlyRefetchIdx++;
  earlyRefetchTimer = setTimeout(() => {
    earlyRefetchTimer = null;
    periodic();
  }, delay);
  setStatus(`sync: early refetch in ${Math.round(delay / 1000)}s (${reason})`);
}

async function periodic() {
  if (syncInFlight) return;
  syncInFlight = true;
  lastPeriodicAt = Date.now();
  try {
    setStatus("sync: fetching manifest…");
    const raw = await fetchManifest();
    setStatus("sync: manifest fetched ✅");

    const manifest = normalizeManifest(raw);
    setStatus(`sync: parsed v${manifest.version} (${manifest.photos.length} photos)…`);

    const res = await syncCache(manifest);
    if (!res.transient) earlyRefetchIdx = 0; // no transient failure: reset recovery backoff
    if (res.changed) currentId = null; // same key may now contain new bytes
    await refreshPlayback();

    // If URLs die before the next hourly poll, refetch shortly before they do.
    if (Number.isFinite(manifest.urlExpiresAt)) {
      const msLeft = manifest.urlExpiresAt * 1000 - Date.now();
      if (msLeft > 0 && msLeft < CONFIG.MANIFEST_POLL_MS) {
        scheduleEarlyRefetch("urls expiring", Math.max(msLeft - 5 * 60 * 1000, 60 * 1000));
      }
    }
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    try {
      await metaSet("last_error", msg);
    } catch {}
    let lastTxt = "never";
    try {
      const last = await metaGet("last_check");
      if (last) lastTxt = new Date(last).toLocaleString();
    } catch {}
    if (isQuotaError(e)) showAlert(`storage full: ${msg} (playing last good; last check ${lastTxt})`);
    else setStatus(`offline: ${msg} (last check ${lastTxt})`);
  } finally {
    syncInFlight = false;
  }
}

// Opportunistic resync when connectivity/visibility returns, instead of
// waiting out the hourly poll. Debounced so event storms can't spam syncs.
function nudgeSync() {
  if (Date.now() - lastPeriodicAt < CONFIG.NUDGE_MIN_GAP_MS) return;
  periodic();
}

// ==================== INIT ====================
// Ask the browser not to evict the offline cache under storage pressure, and
// log usage/quota so a filling disk is diagnosable. Feature-detected.
async function requestPersistentStorage() {
  const storage = typeof navigator !== "undefined" ? navigator.storage : undefined;
  if (!storage) return null;
  let persisted = null;
  let estimate = null;
  try {
    if (typeof storage.persist === "function") persisted = await storage.persist();
  } catch {}
  try {
    if (typeof storage.estimate === "function") estimate = await storage.estimate();
  } catch {}
  try {
    console.log("storage", { persisted, usage: estimate?.usage, quota: estimate?.quota });
  } catch {}
  return { persisted, estimate };
}

async function init() {
  try {
    setStatus("init: starting…");
    setStatus("init: opening db…");
    db = await openDB();
    setStatus("init: db open ✅");
    await requestPersistentStorage();

    // Try playback from cache immediately
    setStatus("init: trying cached playback…");
    await refreshPlayback();

    // Initial sync shortly after boot (doesn't block display)
    setStatus("init: scheduling first sync…");
    setTimeout(periodic, 500);

    // Regular sync
    setInterval(periodic, CONFIG.MANIFEST_POLL_MS);

    // Opportunistic resync on network/visibility recovery
    window.addEventListener("online", nudgeSync);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) nudgeSync();
    });

    // Best-effort wakelock
    if ("wakeLock" in navigator) {
      try {
        await navigator.wakeLock.request("screen");
      } catch {
        // ignore
      }
    }

    // Helpful: show where we think the manifest is coming from
    setStatus(`init: ok (manifest=${MANIFEST_URL})`);
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    setStatus(`fatal: ${msg}`);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
