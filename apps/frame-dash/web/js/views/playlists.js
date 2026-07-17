import { api, toast } from "../api.js";
import { esc, basename } from "../util.js";

export async function renderPlaylists(app) {
  app.innerHTML = `<p class="muted">Loading playlists…</p>`;
  const { playlists } = await api.get("/api/playlists");

  app.innerHTML = `
    <div class="browse-head">
      <h2>Playlists</h2>
      <form id="new-playlist" class="add-bar">
        <input name="name" placeholder="New playlist name" required>
        <button>Create</button>
      </form>
    </div>
    <table class="list">
      <thead><tr><th>Name</th><th>Photos</th><th>Mode</th><th>Updated</th><th></th></tr></thead>
      <tbody>
        ${playlists.map((p) => `
          <tr>
            <td><a href="#/playlist/${p.id}">${esc(p.name)}</a>
                ${p.is_active ? '<span class="badge">ACTIVE</span>' : ""}</td>
            <td>${p.item_count}</td>
            <td>${esc(p.settings.mode)} / ${p.settings.slide_seconds}s</td>
            <td>${esc(p.updated_at)}</td>
            <td><a class="button" href="#/playlist/${p.id}">Edit</a></td>
          </tr>`).join("")}
      </tbody>
    </table>
    ${!playlists.length ? `<p class="muted">No playlists yet — create one above, then add photos from Browse.</p>` : ""}
  `;

  document.getElementById("new-playlist").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name").trim();
    if (!name) return;
    try {
      const created = await api.post("/api/playlists", { name });
      window.location.hash = `#/playlist/${created.id}`;
    } catch (err) {
      toast(err.message, true);
    }
  });
}

export async function renderPlaylistEditor(app, playlistId) {
  app.innerHTML = `<p class="muted">Loading playlist…</p>`;

  let playlist;
  try {
    playlist = await api.get(`/api/playlists/${playlistId}`);
  } catch (err) {
    app.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    return;
  }
  const status = await api.get("/api/status");
  const isActive = status.active && status.active.playlist_id === playlistId;

  // Working copy; nothing persists until Save.
  let items = [...playlist.items];

  const urls = {};
  for (let i = 0; i < items.length; i += 100) {
    const batch = await api.post("/api/preview-urls", { keys: items.slice(i, i + 100) });
    Object.assign(urls, batch.urls);
  }

  function draw() {
    app.innerHTML = `
      <div class="browse-head">
        <h2>${esc(playlist.name)} ${isActive ? '<span class="badge">ACTIVE</span>' : ""}</h2>
        <a href="#/playlists" class="muted">← all playlists</a>
      </div>
      <form id="settings" class="settings">
        <label>Name <input name="name" value="${esc(playlist.name)}"></label>
        <label>Mode
          <select name="mode">
            <option value="sync" ${playlist.settings.mode === "sync" ? "selected" : ""}>sync (frames in lockstep)</option>
            <option value="inventory" ${playlist.settings.mode === "inventory" ? "selected" : ""}>inventory</option>
          </select>
        </label>
        <label>Seconds per slide <input name="slide_seconds" type="number" min="5" value="${playlist.settings.slide_seconds}"></label>
        <label>Start epoch <input name="start_epoch" value="${esc(playlist.settings.start_epoch ?? "now")}" title='"now" or unix seconds'></label>
      </form>
      <ol class="playlist-items" id="items"></ol>
      ${!items.length ? '<p class="muted">Empty — add photos from the Browse tab.</p>' : ""}
      <div class="actions">
        <button id="save">Save</button>
        <button id="dry-run" class="ghost">Preview manifest JSON</button>
        <button id="publish">Publish</button>
        <button id="toggle-active" class="ghost">${isActive ? "Clear active (pause refresh)" : "Set active"}</button>
        <button id="delete" class="danger">Delete playlist</button>
      </div>
      <pre id="preview" hidden></pre>
    `;

    const list = document.getElementById("items");
    items.forEach((key, i) => {
      const li = document.createElement("li");
      li.innerHTML = `
        <img loading="lazy" src="${urls[key] || ""}" alt="">
        <span class="caption">${i + 1}. ${esc(basename(key))}</span>
        <span class="row-actions">
          <button data-act="up" ${i === 0 ? "disabled" : ""}>↑</button>
          <button data-act="down" ${i === items.length - 1 ? "disabled" : ""}>↓</button>
          <button data-act="remove" class="danger">✕</button>
        </span>
      `;
      li.querySelector('[data-act="up"]').onclick = () => { [items[i - 1], items[i]] = [items[i], items[i - 1]]; draw(); };
      li.querySelector('[data-act="down"]').onclick = () => { [items[i + 1], items[i]] = [items[i], items[i + 1]]; draw(); };
      li.querySelector('[data-act="remove"]').onclick = () => { items.splice(i, 1); draw(); };
      list.appendChild(li);
    });

    function settingsFromForm() {
      const form = new FormData(document.getElementById("settings"));
      return {
        name: form.get("name").trim(),
        settings: {
          mode: form.get("mode"),
          slide_seconds: parseInt(form.get("slide_seconds"), 10),
          start_epoch: form.get("start_epoch").trim() || "now",
        },
      };
    }

    async function save() {
      const { name, settings } = settingsFromForm();
      playlist = await api.put(`/api/playlists/${playlistId}`, { name, settings, items });
      items = [...playlist.items];
      toast("Saved");
    }

    document.getElementById("save").onclick = () => save().catch((e) => toast(e.message, true));

    document.getElementById("dry-run").onclick = async () => {
      try {
        await save();
        const result = await api.post(`/api/playlists/${playlistId}/publish`, { dry_run: true });
        const pre = document.getElementById("preview");
        pre.textContent = JSON.stringify(result.manifest, null, 2)
          + (result.skipped_keys.length ? `\n\n// skipped (missing in bucket): ${result.skipped_keys.join(", ")}` : "");
        pre.hidden = false;
      } catch (e) {
        toast(e.message, true);
      }
    };

    document.getElementById("publish").onclick = async () => {
      try {
        await save();
        if (!confirm(`Publish "${playlist.name}" (${items.length} photos) as the live manifest?\n\nTarget: ${status.manifest_key}\nThis is what the frames will play.`)) return;
        const result = await api.post(`/api/playlists/${playlistId}/publish`);
        toast(`Published ${result.version} (${result.photo_count} photos${result.skipped_keys.length ? `, ${result.skipped_keys.length} skipped` : ""})`);
        renderPlaylistEditor(app, playlistId);
      } catch (e) {
        toast(e.message, true);
      }
    };

    document.getElementById("toggle-active").onclick = async () => {
      try {
        await api.put("/api/active", { playlist_id: isActive ? null : playlistId });
        renderPlaylistEditor(app, playlistId);
      } catch (e) {
        toast(e.message, true);
      }
    };

    document.getElementById("delete").onclick = async () => {
      if (!confirm(`Delete playlist "${playlist.name}"? This cannot be undone.`)) return;
      try {
        await api.del(`/api/playlists/${playlistId}`);
        window.location.hash = "#/playlists";
      } catch (e) {
        toast(e.message, true);
      }
    };
  }

  draw();
}
