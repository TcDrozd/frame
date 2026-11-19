import { api } from "../api.js";
import { esc, fmtBytes } from "../util.js";
import { CONFIG } from "../config.js";

export async function renderStatus(host) {
  const app = document.createElement("section");
  host.replaceChildren(app);
  app.innerHTML = `<p class="muted">Loading status…</p>`;
  const status = await api.get("/api/status");
  if (!app.isConnected) return;
  const active = status.active || {};
  const autoOn = status.auto_publish?.mode === "window";
  const retained = !active.snapshot && !!active.previous_snapshot;
  const snapshot = active.snapshot || active.previous_snapshot;
  const version = active.last_version || active.previous_version;
  const epoch = active.resolved_start_epoch ?? active.previous_resolved_start_epoch;
  const pending = status.pending_publication;
  const observedId = status.manifest_head?.publication_id;
  // The protocol suffix is the committed request ID; legacy versions have none.
  const committedId = version?.match(/-([0-9a-f]{32})$/)?.[1];
  const warnings = [];
  if (pending || active.publication_pending) warnings.push("Publication pending — not a completed publication. Canonical bytes may already be new while the committed pointer still describes the previous show.");
  if (observedId && committedId && observedId !== committedId) warnings.push("Divergence: observed canonical publication ID differs from the last committed snapshot.");
  if (status.manifest_observation?.state === "missing") warnings.push("Canonical manifest is confirmed absent; no current canonical publication can be verified.");
  else if (!observedId || !committedId) warnings.push("Canonical publication agreement cannot be verified: publication identity is unavailable. An unavailable HEAD is not proof that the object is absent.");
  const name = active.playlist_name || snapshot?.name || active.playlist_id;
  const selection = active.playlist_id
    ? (active.draft_revision != null
      ? `<a href="#/playlist/${esc(encodeURIComponent(active.playlist_id))}">${esc(name)}</a> (draft revision ${esc(active.draft_revision)})`
      : `${esc(name)} — draft unavailable; published snapshot retained`)
    : autoOn
      ? "none — automatic selection enabled on the next scheduled run"
      : "none — scheduled refresh is idle; frames retain the last published show";
  app.innerHTML = `
    <h2>Status</h2>
    ${warnings.length ? `<p class="error" role="alert">${warnings.map(esc).join(" ")}</p>` : ""}
    <dl class="status">
      <dt>Selection</dt><dd>${selection}</dd>
      <dt>Last published snapshot</dt><dd>${snapshot
        ? `${esc(snapshot.name || "Automatic window")} — revision ${esc(active.published_revision ?? snapshot.revision ?? "not applicable")}${retained ? " (retained after clearing selection)" : ""}; ${esc(snapshot.items?.length ?? active.photo_count ?? "unknown")} photos`
        : "No immutable snapshot recorded (legacy or unpublished state)"}</dd>
      <dt>Auto-publish</dt><dd>${autoOn ? `window of ${esc(status.auto_publish.window_size)} (takes over whenever no playlist is selected)` : "off"}</dd>
      <dt>Last published</dt><dd>${version
        ? `${esc(active.last_published_at || "Time not recorded")} (version ${esc(version)})`
        : "No publication recorded"}</dd>
      ${epoch != null ? `<dt>Playback anchored at</dt><dd>${esc(new Date(epoch * 1000).toLocaleString())} (epoch ${esc(epoch)})</dd>` : ""}
      <dt>Pending publication</dt><dd>${pending
        ? `${esc(pending.request_id)} — playlist ${esc(pending.playlist_id || "automatic")}, revision ${esc(pending.published_revision ?? "not applicable")}; awaiting commit`
        : active.publication_pending ? "Reservation pending; details unavailable" : "none"}</dd>
      <dt>Committed snapshot publication ID</dt><dd>${esc(committedId || "unavailable")}</dd>
      <dt>Observed canonical publication ID</dt><dd>${esc(observedId || "unavailable")}</dd>
      <dt>Manifest object</dt><dd><code>${esc(status.manifest_key)}</code> — ${status.manifest_head
        ? `${esc(fmtBytes(status.manifest_head.size))}, modified ${esc(status.manifest_head.last_modified)}`
        : status.manifest_observation?.state === "error"
          ? `Observation failed (${esc(status.manifest_observation.code || "unknown error")}); canonical state unknown`
          : status.manifest_observation?.state === "missing"
            ? `Not found (${esc(status.manifest_observation.code || "404")})`
            : "HEAD unavailable (missing object or read failure)"}</dd>
      <dt>Manifest URL (for clients)</dt><dd><a href="${esc(CONFIG.manifestUrl)}" target="_blank" rel="noopener noreferrer"><code>${esc(CONFIG.manifestUrl)}</code></a></dd>
      <dt>Presign expiry</dt><dd>${status.presign_expiry_seconds / 3600} hours (scheduled refresh requires a selected snapshot or auto-publish)</dd>
    </dl>`;
}
