import { api, toast } from "../api.js";
import { esc, basename } from "../util.js";

// Each entry owns its DOM and state; pagination only appends to that entry.
let state = null;
async function previewUrls(keys) {
  const urls = {};
  for (let i = 0; i < keys.length; i += 100) {
    const batch = await api.post("/api/preview-urls", { keys: keys.slice(i, i + 100) });
    Object.assign(urls, batch.urls);
  }
  return urls;
}
function breadcrumb(prefix) {
  let acc = "";
  return prefix.replace(/\/$/, "").split("/").map(part => {
    acc += part + "/";
    return `<a href="#/browse?prefix=${encodeURIComponent(acc)}">${esc(part)}</a>`;
  }).join(" / ");
}

export async function renderBrowse(app, params, append = false) {
  const prefix = params.get("prefix") || "";
  if (!append || !state || state.prefix !== prefix) {
    const root = document.createElement("section");
    root.innerHTML = `<p class="muted">Loading ${esc(prefix || "photos/")}…</p>`;
    app.replaceChildren(root);
    state = { prefix, root, folders: new Set(), photos: [], seen: new Set(), nextToken: null, selected: new Set(), urls: {}, loading: false, adding: false };
  }
  const view = state;
  const root = view.root;
  if (view.loading || view.adding) return;
  view.loading = true;
  root.querySelector("#load-more")?.setAttribute("disabled", "");
  root.querySelector("#playlist-pick")?.setAttribute("disabled", "");
  try {
    const qs = new URLSearchParams();
    if (prefix) qs.set("prefix", prefix);
    if (view.nextToken) qs.set("token", view.nextToken);
    const page = await api.get(`/api/browse?${qs}`);
    const seen = new Set(view.seen);
    const fresh = page.photos.filter(p => {
      if (seen.has(p.key)) return false;
      seen.add(p.key);
      return true;
    });
    const [urls, playlists] = await Promise.all([
      previewUrls(fresh.map(p => p.key)),
      api.get("/api/playlists").then(r => r.playlists),
    ]);
    if (state !== view || !root.isConnected) return;
    // Commit the page only after all reads succeed; retry cannot lose photos.
    view.seen = seen;
    view.photos.push(...fresh);
    page.folders.forEach(folder => view.folders.add(folder));
    view.nextToken = page.next_token || null;
    Object.assign(view.urls, urls);
    root.innerHTML = `
      <div class="browse-head"><div class="crumbs">${breadcrumb(page.prefix)}</div>
        <div class="add-bar"><span id="sel-count" class="muted" role="status"></span>
          <select id="playlist-pick" aria-label="Add selected photos to playlist">
            <option value="">Add selected to…</option>
            ${playlists.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}
          </select></div></div>
      ${view.folders.size ? `<div class="folders">${[...view.folders].map(folder => `<a class="folder" href="#/browse?prefix=${encodeURIComponent(folder)}">📁 ${esc(basename(folder.replace(/\/$/, "")))}</a>`).join("")}</div>` : ""}
      <div class="grid" id="photo-grid"></div>
      ${view.nextToken ? '<button id="load-more">Load more</button>' : ""}
      ${!view.folders.size && !view.photos.length ? '<p class="muted">No photos here.</p>' : ""}`;
    const grid = root.querySelector("#photo-grid");
    function updateSelCount() {
      root.querySelector("#sel-count").textContent = view.selected.size ? `${view.selected.size} selected` : "";
    }
    for (const photo of view.photos) {
      const card = document.createElement("label");
      card.className = "card" + (view.selected.has(photo.key) ? " selected" : "");
      card.innerHTML = `<input type="checkbox" ${view.selected.has(photo.key) ? "checked" : ""}>
        <img loading="lazy" src="${esc(view.urls[photo.key] || "")}" alt="${esc(basename(photo.key))}">
        <span class="caption">${esc(basename(photo.key))}</span>`;
      card.querySelector("input").addEventListener("change", e => {
        e.target.checked ? view.selected.add(photo.key) : view.selected.delete(photo.key);
        card.classList.toggle("selected", e.target.checked);
        updateSelCount();
      });
      grid.appendChild(card);
    }
    updateSelCount();
    root.querySelector("#load-more")?.addEventListener("click", () => renderBrowse(app, params, true));
    root.querySelector("#playlist-pick").addEventListener("change", async e => {
      if (view.adding || view.loading) return;
      const picker = e.target;
      const pid = picker.value;
      picker.value = "";
      const selected = [...view.selected];
      if (!pid || !selected.length) {
        if (!selected.length) toast("Select some photos first", true);
        return;
      }
      view.adding = true;
      picker.disabled = true;
      root.querySelector("#load-more")?.setAttribute("disabled", "");
      try {
        const path = `/api/playlists/${encodeURIComponent(pid)}`;
        const playlist = await api.get(path);
        if (!Number.isInteger(playlist.revision)) throw new Error("Playlist revision unavailable. Reload before adding photos.");
        const existing = new Set(playlist.items);
        const additions = selected.filter(key => !existing.has(key));
        if (additions.length) await api.put(path, { expected_revision: playlist.revision, items: [...playlist.items, ...additions] });
        selected.forEach(key => view.selected.delete(key));
        root.querySelectorAll(".card").forEach((card, i) => {
          const checked = view.selected.has(view.photos[i].key);
          card.querySelector("input").checked = checked;
          card.classList.toggle("selected", checked);
        });
        updateSelCount();
        toast(additions.length ? `Added ${additions.length} photo(s) to "${playlist.name}"` : `Already in "${playlist.name}" — nothing added`);
      } catch (err) {
        toast(err.status === 409 ? "Revision conflict: playlist changed elsewhere. Selection kept; reload the playlist and review before retrying." : err.message, true);
      } finally {
        view.adding = false;
        picker.disabled = false;
        root.querySelector("#load-more")?.removeAttribute("disabled");
      }
    });
  } catch (err) {
    if (root.isConnected) {
      if (!append) root.innerHTML = `<p class="error">${esc(err.message)}</p>`;
      else toast(err.message, true);
    }
  } finally {
    view.loading = false;
    root.querySelector("#load-more")?.removeAttribute("disabled");
    root.querySelector("#playlist-pick")?.removeAttribute("disabled");
  }
}
