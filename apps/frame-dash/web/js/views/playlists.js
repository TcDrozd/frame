import { api, toast } from "../api.js";
import { esc, basename } from "../util.js";
import { setDraftGuard } from "../draft.js";

export async function renderPlaylists(host) {
  const app = document.createElement("section");
  host.replaceChildren(app);
  app.innerHTML = `<p class="muted">Loading playlists…</p>`;
  const { playlists } = await api.get("/api/playlists");
  app.innerHTML = `
    <div class="browse-head"><h2>Playlists</h2>
      <form id="new-playlist" class="add-bar">
        <input name="name" aria-label="New playlist name" placeholder="New playlist name" required><button>Create</button>
      </form>
    </div>
    <table class="list">
      <thead><tr><th>Name</th><th>Photos</th><th>Mode</th><th>Updated</th><th></th></tr></thead>
      <tbody>${playlists.map((p) => `
        <tr><td><a href="#/playlist/${esc(encodeURIComponent(p.id))}">${esc(p.name)}</a>
          ${p.is_active ? '<span class="badge">ACTIVE</span>' : ""}</td>
          <td>${esc(p.item_count)}</td><td>${esc(p.settings.mode)} / ${esc(p.settings.slide_seconds)}s</td>
          <td>${esc(p.updated_at)}</td><td><a class="button" href="#/playlist/${esc(encodeURIComponent(p.id))}">Edit</a></td></tr>`).join("")}</tbody>
    </table>
    ${!playlists.length ? '<p class="muted">No playlists yet — create one above, then add photos from Browse.</p>' : ""}`;
  app.querySelector("#new-playlist").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name").trim();
    if (!name) return;
    try {
      const created = await api.post("/api/playlists", { name });
      window.location.hash = `#/playlist/${encodeURIComponent(created.id)}`;
    } catch (err) { toast(err.message, true); }
  });
}

export async function renderPlaylistEditor(host, playlistId) {
  const app = document.createElement("section");
  host.replaceChildren(app);
  app.innerHTML = `<p class="muted">Loading playlist…</p>`;
  const path = `/api/playlists/${encodeURIComponent(playlistId)}`;
  let playlist, status;
  try {
    [playlist, status] = await Promise.all([api.get(path), api.get("/api/status")]);
  } catch (err) {
    app.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    return;
  }
  if (!app.isConnected) return;
  let isActive = status.active && status.active.playlist_id === playlistId;
  const autoOn = status.auto_publish && status.auto_publish.mode === "window";
  const copy = (p) => ({ name: p.name, settings: { ...p.settings }, items: [...p.items] });
  let draft = copy(playlist);
  let busy = false;
  // Kept in this editor until acknowledged or superseded by a different intent.
  let pendingPublish = null;
  let publicationState = status.manifest_head?.publication_id
    ? `Observed canonical publication ID: ${status.manifest_head.publication_id}`
    : "Canonical publication ID unavailable.";
  const dirty = () => JSON.stringify(draft) !== JSON.stringify(copy(playlist));
  setDraftGuard(() => app.isConnected && (busy || dirty() || !!pendingPublish));
  const urls = {};
  for (let i = 0; i < draft.items.length; i += 100) {
    const batch = await api.post("/api/preview-urls", { keys: draft.items.slice(i, i + 100) });
    Object.assign(urls, batch.urls);
  }

  function updateDirty() {
    if (pendingPublish && (JSON.stringify(draft) !== pendingPublish.draft || playlist.revision !== pendingPublish.body.expected_revision)) pendingPublish = null;
    const label = app.querySelector("#draft-state");
    if (label) label.textContent = dirty() ? "Unsaved changes — Save explicitly or publish after confirmation." : `Saved revision ${playlist.revision}`;
    const pre = app.querySelector("#preview");
    if (pre) pre.hidden = true; // A preview is only valid for the draft it rendered.
    const publish = app.querySelector("#publish");
    if (publish) publish.textContent = pendingPublish ? "Retry unconfirmed publish" : "Review and publish";
  }
  function epochValue(value) {
    const text = value.trim();
    // Do not let Number() silently accept empty, exponent, hex or fractional input.
    return /^[0-9]+$/.test(text) && Number.isSafeInteger(Number(text)) && Number(text) <= 253402300799
      ? Number(text) : text;
  }
  function readForm() {
    const form = new FormData(app.querySelector("#settings"));
    draft.name = form.get("name");
    draft.settings = {
      mode: form.get("mode"), slide_seconds: Number(form.get("slide_seconds")),
      start_epoch: epochValue(form.get("start_epoch")),
    };
    updateDirty();
  }
  function snapshot() {
    readForm();
    const result = copy(draft);
    result.name = result.name.trim();
    if (result.settings.start_epoch !== "now" && !Number.isInteger(result.settings.start_epoch)) {
      throw new Error('Start epoch must be "now" or an integer from 0 to 253402300799.');
    }
    if (!result.name || !Number.isInteger(result.settings.slide_seconds) || result.settings.slide_seconds < 5) {
      throw new Error("Enter a playlist name and whole seconds per slide (at least 5).");
    }
    if (!Number.isInteger(playlist.revision)) throw new Error("Playlist revision unavailable. Reload before saving or publishing.");
    return result;
  }
  function failure(error) {
    // A 409 can mean a stale edit, OR that this request finished an earlier
    // pending publication first, so frames may have changed. Show the server's
    // reason and never claim nothing was published.
    toast(error.status === 409
      ? `Conflict: ${error.message || "this playlist changed elsewhere"}. Your draft is kept; reload to see current and live state before retrying.`
      : error.message, true);
  }
  async function run(action) {
    if (busy) return;
    busy = true;
    app.setAttribute("aria-busy", "true");
    app.querySelectorAll("input, select, button").forEach(el => { el.disabled = true; });
    try { await action(); } catch (error) { failure(error); }
    finally {
      busy = false;
      app.removeAttribute("aria-busy");
      // Do not redraw: preserve a preview, errors, form values, and keyboard focus.
      app.querySelectorAll("input, select, button").forEach(el => { el.disabled = false; });
      app.querySelectorAll('[data-act="up"]').forEach((el, i) => { el.disabled = i === 0; });
      app.querySelectorAll('[data-act="down"]').forEach((el, i) => { el.disabled = i === draft.items.length - 1; });
    }
  }
  async function save(reviewed) {
    pendingPublish = null; // An explicit save starts a new revision/intent, never a publish retry.
    const saved = await api.put(path, { ...reviewed, expected_revision: playlist.revision });
    if (!Number.isInteger(saved.revision)) throw new Error("Save returned no revision. Reload and review before publishing.");
    playlist = saved;
    draft = copy(saved);
    draw();
    toast("Saved");
    return saved;
  }
  function draw() {
    app.innerHTML = `
      <div class="browse-head"><h2>${esc(draft.name)} ${isActive ? '<span class="badge">ACTIVE</span>' : ""}</h2>
        <a href="#/playlists" class="muted">← all playlists</a></div>
      <p id="draft-state" class="muted" role="status"></p>
      <p id="publication-state" class="muted" role="status">${esc(publicationState)}</p>
      <form id="settings" class="settings">
        <label>Name <input name="name" value="${esc(draft.name)}" required></label>
        <label>Mode <select name="mode">
          <option value="sync" ${draft.settings.mode === "sync" ? "selected" : ""}>sync (frames in lockstep)</option>
          <option value="inventory" ${draft.settings.mode === "inventory" ? "selected" : ""}>inventory</option>
        </select></label>
        <label>Seconds per slide <input name="slide_seconds" type="number" min="5" step="1" value="${esc(draft.settings.slide_seconds)}" required></label>
        <label>Start epoch <input name="start_epoch" value="${esc(draft.settings.start_epoch ?? "now")}" title='"now" or unix seconds'></label>
      </form>
      <ol class="playlist-items" id="items"></ol>
      ${!draft.items.length ? '<p class="muted">Empty — add photos from the Browse tab.</p>' : ""}
      <div class="actions">
        <button id="save">Save</button><button id="dry-run" class="ghost">Preview manifest JSON (no save)</button>
        <button id="publish">Review and publish</button>
        <button id="toggle-active" class="ghost">${isActive ? (autoOn ? "Clear active (hand back to auto)" : "Clear active (pause refresh)") : "Review and activate (publishes)"}</button>
        <button id="delete" class="danger">Delete playlist</button>
      </div><pre id="preview" hidden></pre>`;
    const form = app.querySelector("#settings");
    form.addEventListener("submit", e => e.preventDefault());
    form.addEventListener("input", readForm);
    form.addEventListener("change", readForm);
    const list = app.querySelector("#items");
    draft.items.forEach((key, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<img loading="lazy" src="${esc(urls[key] || "")}" alt="">
        <span class="caption">${i + 1}. ${esc(basename(key))}</span>
        <span class="row-actions">
          <button type="button" data-act="up" aria-label="${esc(`Move ${basename(key)} up`)}" ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" data-act="down" aria-label="${esc(`Move ${basename(key)} down`)}" ${i === draft.items.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" data-act="remove" aria-label="${esc(`Remove ${basename(key)}`)}" class="danger">✕</button>
        </span>`;
      function change(action) {
        if (busy) return;
        readForm(); // Capture even edits not yet delivered as input events.
        action();
        draw();
        const next = Math.min(i, draft.items.length - 1);
        app.querySelectorAll('[data-act="remove"]')[next]?.focus();
      }
      li.querySelector('[data-act="up"]').onclick = () => change(() => { [draft.items[i - 1], draft.items[i]] = [draft.items[i], draft.items[i - 1]]; });
      li.querySelector('[data-act="down"]').onclick = () => change(() => { [draft.items[i + 1], draft.items[i]] = [draft.items[i], draft.items[i + 1]]; });
      li.querySelector('[data-act="remove"]').onclick = () => change(() => { draft.items.splice(i, 1); });
      list.appendChild(li);
    });
    app.querySelector("#save").onclick = () => {
      let reviewed;
      try { reviewed = snapshot(); } catch (error) { failure(error); return; }
      return run(() => save(reviewed));
    };
    app.querySelector("#dry-run").onclick = () => {
      let reviewed;
      try { reviewed = snapshot(); } catch (error) { failure(error); return; }
      return run(async () => {
        const result = await api.post(`${path}/publish`, { dry_run: true, expected_revision: playlist.revision, draft: reviewed });
        const pre = app.querySelector("#preview");
        pre.textContent = JSON.stringify(result.manifest, null, 2)
          + (result.skipped_keys.length ? `\n\n// skipped (missing in bucket): ${result.skipped_keys.join(", ")}` : "");
        pre.hidden = false;
      });
    };
    app.querySelector("#publish").onclick = () => {
      if (busy) return;
      let reviewed;
      try { reviewed = snapshot(); } catch (error) { failure(error); return; }
      const retry = pendingPublish;
      if (!confirm(retry
        ? `Retry the unconfirmed publish of "${reviewed.name}" at revision ${retry.body.expected_revision}?\nTarget: ${status.manifest_key}\nThis retries the same request without saving or restarting an already completed publish.`
        : `Save and publish "${reviewed.name}" (${reviewed.items.length} photos in the displayed order) as the live manifest?\n\nMode: ${reviewed.settings.mode}; seconds: ${reviewed.settings.slide_seconds}; start: ${reviewed.settings.start_epoch}\nTarget: ${status.manifest_key}\nThis saves the reviewed draft and changes what the frames play.`)) return;
      return run(async () => {
        if (!retry) {
          const requestId = Array.from(window.crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
          const saved = await save(reviewed);
          pendingPublish = {
            draft: JSON.stringify(copy(saved)),
            body: { dry_run: false, restart: true, expected_revision: saved.revision, request_id: requestId },
          };
        }
        // A concurrent write between save and publish must yield 409, not publish a different revision.
        let result;
        try {
          result = await api.post(`${path}/publish`, pendingPublish.body);
          // HTTP success alone cannot acknowledge intent: validate before releasing
          // the durable-retry key or declaring activation successful.
          if (!result || typeof result.version !== "string" || !result.version.trim()
              || result.request_id !== pendingPublish.body.request_id
              || !result.version.endsWith(`-${result.request_id}`)
              || result.manifest_key !== status.manifest_key || result.dry_run !== false
              || !Number.isInteger(result.photo_count) || result.photo_count < 1
              || !Array.isArray(result.skipped_keys) || !result.skipped_keys.every(key => typeof key === "string")) {
            throw new Error("Incomplete or invalid publication acknowledgement");
          }
        } catch (error) {
          if (!error.status || error.status >= 500) {
            updateDirty();
            throw new Error(`Publication outcome unknown: ${error.message}. Retry unconfirmed publish without editing or saving to reuse the same request. Leaving this editor loses the retry key.`);
          }
          pendingPublish = null;
          updateDirty();
          throw error;
        }
        pendingPublish = null;
        isActive = true;
        publicationState = `Acknowledged publication ID: ${result.request_id}; version ${result.version}.`;
        // A status-read failure does not undo a validated durable receipt.
        try {
          status = await api.get("/api/status");
          const observed = status.manifest_head?.publication_id;
          publicationState += observed
            ? ` Observed canonical publication ID: ${observed}.${observed !== result.request_id ? " Canonical publication differs (a newer publication or divergence); refresh status for details." : ""}`
            : " Canonical publication observation unavailable.";
          isActive = status.active ? status.active.playlist_id === playlistId : isActive;
        } catch {
          publicationState += " Status refresh unavailable; acknowledged publication ID retained, current canonical state unknown.";
        }
        draw();
        toast(`Published ${result.version} (${result.photo_count} photos${result.skipped_keys.length ? `, ${result.skipped_keys.length} skipped` : ""})`);
      });
    };
    app.querySelector("#toggle-active").onclick = () => {
      if (!isActive) return app.querySelector("#publish").onclick();
      if (!confirm("Clear active playlist? Unsaved edits are not saved or used.")) return;
      return run(async () => {
        await api.put("/api/active", { playlist_id: isActive ? null : playlistId });
        isActive = !isActive;
        draw(); // Keep the working draft, including settings.
      });
    };
    app.querySelector("#delete").onclick = () => {
      if (!confirm(`Delete playlist "${draft.name}" and discard any draft changes? This cannot be undone.`)) return;
      return run(async () => {
        await api.del(path, { expected_revision: playlist.revision });
        draft = copy(playlist);
        window.location.hash = "#/playlists";
      });
    };
    updateDirty();
    if (busy) app.querySelectorAll("input, select, button").forEach(el => { el.disabled = true; });
  }
  draw();
}
