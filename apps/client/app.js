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
 * - Early manifest refetch hint on expired presigned URLs (403/404)
 */

// ==================== CONFIG ====================
const CONFIG = {
  // Default manifest URL (fallback). You can override at runtime:
  //   http://<host>:8000/?manifest=https://.../manifest.json
  DEFAULT_MANIFEST_URL: "https://trevor-shared-photo-stream.s3.us-east-1.amazonaws.com/manifest.json",

  // How often to poll the manifest for updates (ms)
  MANIFEST_POLL_MS: 60 * 60 * 1000, // 1 hour

  // Max cached photos (fallback). If you later add schema2, you can drive this from manifest.
  MAX_CACHED: 500,

  // Transition fade ms (must match CSS transition)
  FADE_MS: 2000,

  // If manifest does NOT include timing fields, use this rotation time:
  DEFAULT_SLIDE_MS: 60 * 60 * 1000, // 1 hour

  // Show debug status overlay
  DISPLAY_STATUS: true,

  // If true, append cache-bust to manifest fetches
  NO_CACHE_MANIFEST: true,
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
function setStatus(msg) {
  try {
    if (!CONFIG.DISPLAY_STATUS) return;
    const el = document.getElementById("status");
    if (el) el.textContent = msg;
    // Mirror in URL hash so you can see it without DevTools / share screenshot context.
    window.location.hash = encodeURIComponent(String(msg)).slice(0, 180);
  } catch {}
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
const DB_VER = 1;
const STORES = {
  photos: "photos", // key: id
  meta: "meta", // key: key
};

let db;

function idbReq(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VER);

    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains(STORES.photos)) {
        const s = d.createObjectStore(STORES.photos, { keyPath: "id" });
        s.createIndex("ts", "ts", { unique: false });
      }
      if (!d.objectStoreNames.contains(STORES.meta)) {
        d.createObjectStore(STORES.meta, { keyPath: "key" });
      }
    };

    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function tx(storeName, mode, fn) {
  const t = db.transaction([storeName], mode);
  const store = t.objectStore(storeName);
  const out = await fn(store);

  await new Promise((resolve, reject) => {
    t.oncomplete = () => resolve();
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  });

  return out;
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

async function photoPut({ id, blob, mime, name, sha256, ts }) {
  return tx(STORES.photos, "readwrite", async (s) => {
    await idbReq(s.put({ id, blob, mime, name, sha256, ts }));
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

async function photoDelete(id) {
  return tx(STORES.photos, "readwrite", async (s) => {
    await idbReq(s.delete(id));
  });
}

async function trimCache(max) {
  const all = await photoGetAll();
  if (all.length <= max) return;
  all.sort((a, b) => (a.ts || 0) - (b.ts || 0)); // oldest first
  const toDel = all.slice(0, all.length - max);
  for (const p of toDel) await photoDelete(p.id);
}

// Stable signature so we can detect changes even if `version` is unchanged.
function manifestSig(m) {
  const slim = {
    version: m.version,
    startEpoch: m.startEpoch,
    slideSeconds: m.slideSeconds,
    mode: m.mode,
    photos: (m.photos || []).map((p) => ({ id: p.id, url: p.url, sha256: p.sha256 })),
  };
  return JSON.stringify(slim);
}

// ==================== MANIFEST ====================
async function fetchManifest() {
  const base = new URL(window.location.href);
  const manifestUrl = new URL(MANIFEST_URL, base);

  const url = CONFIG.NO_CACHE_MANIFEST
    ? `${manifestUrl.toString()}?t=${Date.now()}`
    : manifestUrl.toString();

  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`manifest fetch failed: ${res.status}`);
  return res.json();
}

function normalizeManifest(raw) {
  const version = String(raw.version ?? "0");

  const photos = Array.isArray(raw.photos)
    ? raw.photos
    : Array.isArray(raw.images)
      ? raw.images.map((id) => ({ id }))
      : [];

  const normPhotos = photos.map((p) => ({
    id: String(p.id),
    url: p.url ? String(p.url) : undefined,
    name: p.name ? String(p.name) : undefined,
    sha256: p.sha256 ? String(p.sha256) : undefined,
    bytes: Number.isFinite(p.bytes) ? p.bytes : undefined,
  }));

  const startEpoch = raw.start_epoch != null ? Number(raw.start_epoch) : undefined;
  const slideSeconds = raw.slide_seconds != null ? Number(raw.slide_seconds) : undefined;
  const mode = raw.mode ? String(raw.mode) : undefined;

  return { version, photos: normPhotos, startEpoch, slideSeconds, mode };
}

// ==================== DOWNLOAD ====================
function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function downloadToCache(entry) {
  if (!entry.url) return false;

  const base = new URL(window.location.href);
  const photoUrl = new URL(entry.url, base).toString();

  const res = await fetch(photoUrl);

  if (!res.ok) {
    // Presigned URL expired or object missing: mark for early manifest check
    if (res.status === 403 || res.status === 404) {
      try {
        await metaSet("force_manifest_check", Date.now());
      } catch {}
    }
    throw new Error(`download failed ${entry.id}: ${res.status}`);
  }

  const blob = await res.blob();
  const mime = blob.type || "application/octet-stream";

  await photoPut({
    id: entry.id,
    blob,
    mime,
    name: entry.name,
    sha256: entry.sha256,
    ts: Date.now(),
  });

  return true;
}

async function syncCache(manifest) {
  setStatus("sync: checking…");

  const cached = await metaGet("manifest");
  const cachedVer = cached?.version;

  const newSig = manifestSig(manifest);
  const cachedSig = await metaGet("manifest_sig");

  // If version matches AND signature matches, no changes.
  if (cachedVer && cachedVer === manifest.version && cachedSig === newSig) {
    await metaSet("last_check", Date.now());
    setStatus(`sync: up to date (v${manifest.version}, ${manifest.photos.length})`);
    return { changed: false };
  }

  // Signature changed but version same (timing/order/etc)
  if (cachedVer && cachedVer === manifest.version && cachedSig !== newSig) {
    setStatus(`sync: config changed (v${manifest.version})`);
  }

  const cachedPhotos = await photoGetAll();
  const cachedIds = new Set(cachedPhotos.map((p) => p.id));
  const manifestIds = new Set(manifest.photos.map((p) => p.id));

  const toDelete = cachedPhotos.filter((p) => !manifestIds.has(p.id));
  for (const p of toDelete) await photoDelete(p.id);

  const toDownload = manifest.photos.filter((p) => !cachedIds.has(p.id));

  let ok = 0;
  let fail = 0;
  const CONCURRENCY = 4;
  const queue = [...toDownload];

  async function worker() {
    while (queue.length) {
      const item = queue.shift();
      try {
        setStatus(`sync: dl ${ok + fail + 1}/${toDownload.length}…`);
        await downloadToCache(item);
        ok++;
      } catch (e) {
        fail++;
        setStatus(`sync: dl failed (${fail})…`);
        await sleep(500);
      }
    }
  }

  const workers = Array.from({ length: Math.min(CONCURRENCY, toDownload.length) }, () => worker());
  await Promise.all(workers);

  await trimCache(CONFIG.MAX_CACHED);

  await metaSet("manifest", manifest);
  await metaSet("manifest_sig", manifestSig(manifest));
  await metaSet("last_sync", Date.now());
  await metaSet("last_check", Date.now());

  setStatus(`sync: done v${manifest.version} (+${ok}/-${toDelete.length}, fail ${fail})`);
  return { changed: true, ok, fail, deleted: toDelete.length };
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
}

// ==================== LOOP ====================
async function buildPlayOrder(manifest) {
  const cached = await photoGetAll();
  const map = new Map(cached.map((p) => [p.id, p]));
  const ordered = manifest.photos.map((p) => map.get(p.id)).filter(Boolean);
  return ordered.length ? ordered.map((p) => p.id) : cached.map((p) => p.id);
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

  if (playIds.length === 0) {
    setStatus("playback: no cached photos yet");
    return;
  }

  await tick();
  setStatus(`playback: ${inSyncMode(m) ? "sync" : "mvp"} (${playIds.length} cached, v${m.version})`);
}

// ==================== PERIODIC SYNC ====================
async function periodic() {
  try {
    setStatus("sync: fetching manifest…");
    const raw = await fetchManifest();
    setStatus("sync: manifest fetched ✅");

    const manifest = normalizeManifest(raw);
    setStatus(`sync: parsed v${manifest.version} (${manifest.photos.length} photos)…`);

    const res = await syncCache(manifest);
    if (res.changed) {
      setStatus("playback: refreshing…");
      await refreshPlayback();
    } else {
      // even if not changed, try playback from cache (safe)
      await refreshPlayback();
    }
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    try {
      await metaSet("last_error", msg);
    } catch {}
    const last = await metaGet("last_check");
    const lastTxt = last ? new Date(last).toLocaleString() : "never";
    setStatus(`offline: ${msg} (last check ${lastTxt})`);
  }
}

// ==================== INIT ====================
async function init() {
  try {
    setStatus("init: starting…");
    setStatus("init: opening db…");
    db = await openDB();
    setStatus("init: db open ✅");

    // Try playback from cache immediately
    setStatus("init: trying cached playback…");
    await refreshPlayback();

    // Initial sync shortly after boot (doesn't block display)
    setStatus("init: scheduling first sync…");
    setTimeout(periodic, 500);

    // Regular sync
    setInterval(periodic, CONFIG.MANIFEST_POLL_MS);

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
