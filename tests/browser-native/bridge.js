// Test bridge only. Production cache/decoder/IDB implementations are unchanged.
window.nativeAcceptance = {
  ready: (async () => { db = await openDB(); })(),
  async sync(raw) {
    await this.ready;
    try { return {result: await syncCache(normalizeManifest(raw))}; }
    catch (error) { return {error: String(error), name: error?.name}; }
  },
  async snapshot() {
    await this.ready;
    const photos = await photoGetAll();
    const metadata = await tx(STORES.meta, 'readonly', s => idbReq(s.getAll()));
    const rows = await Promise.all(photos.map(async row => ({
      ...row, blob: {type: row.blob.type, bytes: Array.from(new Uint8Array(await row.blob.arrayBuffer()))}
    })));
    return {photos: rows.sort((a, b) => a.id.localeCompare(b.id)),
      meta: metadata.sort((a, b) => a.key.localeCompare(b.key)),
      order: await buildPlayOrder(await metaGet('manifest'))};
  },
  async decode(url) {
    const blob = await (await fetch(url)).blob();
    const image = new Image();
    const objectURL = URL.createObjectURL(blob);
    image.src = objectURL;
    try { await image.decode(); return {decoded: true, width: image.naturalWidth, height: image.naturalHeight}; }
    catch (error) { return {decoded: false, error: String(error)}; }
    finally { URL.revokeObjectURL(objectURL); }
  },
  armAbort() {
    // All operations still use native IDB requests/transactions. Abort only
    // AFTER successful writes/deletions, including the final metadata put.
    // No fake IDB, replacement transaction, or synchronous put exception.
    const originalPut = IDBObjectStore.prototype.put;
    const originalDelete = IDBObjectStore.prototype.delete;
    const evidence = this.abortEvidence = {events: [], aborted: false};
    function instrument(original, kind) {
      return function(...args) {
        const request = original.apply(this, args);
        const transaction = this.transaction;
        const store = this.name;
        const key = kind === 'put' ? (args[0].id ?? args[0].key) : args[0];
        if (transaction.mode === 'readwrite' && transaction.objectStoreNames.contains('photos') &&
            transaction.objectStoreNames.contains('meta')) {
          request.addEventListener('success', () => {
            evidence.events.push({kind, store, key, success: true});
            if (store === 'meta' && kind === 'put' && key === 'last_check') {
              transaction.addEventListener('abort', () => { evidence.aborted = true; });
              transaction.abort();
              IDBObjectStore.prototype.put = originalPut;
              IDBObjectStore.prototype.delete = originalDelete;
            }
          });
        }
        return request;
      };
    }
    IDBObjectStore.prototype.put = instrument(originalPut, 'put');
    IDBObjectStore.prototype.delete = instrument(originalDelete, 'delete');
  }
};
