import { api } from "../api.js";
import { esc, fmtBytes } from "../util.js";
import { CONFIG } from "../config.js";

export async function renderStatus(app) {
  app.innerHTML = `<p class="muted">Loading status…</p>`;
  const status = await api.get("/api/status");
  const active = status.active;

  app.innerHTML = `
    <h2>Status</h2>
    <dl class="status">
      <dt>Active playlist</dt>
      <dd>${active && active.playlist_id
        ? `<a href="#/playlist/${active.playlist_id}">${esc(active.playlist_name || active.playlist_id)}</a>`
        : '<span class="muted">none — scheduled refresh is idle</span>'}</dd>

      <dt>Last published</dt>
      <dd>${active && active.last_published_at
        ? `${esc(active.last_published_at)} (version ${esc(active.last_version)})`
        : '<span class="muted">never</span>'}</dd>

      ${active && active.resolved_start_epoch ? `
      <dt>Playback anchored at</dt>
      <dd>${new Date(active.resolved_start_epoch * 1000).toLocaleString()} (epoch ${active.resolved_start_epoch})</dd>` : ""}

      <dt>Manifest object</dt>
      <dd>${status.manifest_head
        ? `<code>${esc(status.manifest_key)}</code> — ${fmtBytes(status.manifest_head.size)}, modified ${esc(status.manifest_head.last_modified)}`
        : `<code>${esc(status.manifest_key)}</code> — <span class="muted">not found in bucket</span>`}</dd>

      <dt>Manifest URL (for clients)</dt>
      <dd><a href="${CONFIG.manifestUrl}" target="_blank"><code>${esc(CONFIG.manifestUrl)}</code></a></dd>

      <dt>Presign expiry</dt>
      <dd>${status.presign_expiry_seconds / 3600} hours (re-published on schedule to stay fresh)</dd>
    </dl>
  `;
}
