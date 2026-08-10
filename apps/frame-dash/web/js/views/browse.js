import { api, toast } from "../api.js";
import { esc, basename } from "../util.js";

// Pagination state survives "Load more" only; every fresh entry to the view
// resets it (re-entering with stale accumulated photos would duplicate cards).
let state = null;

function resetState(prefix) {
  state = { prefix, photos: [], seen: new Set(), nextToken: null, selected: new Set(), urls: {} };
}

async function previewUrls(keys) {
  if (!keys.length) return {};
  const urls = {};
  for (let i = 0; i < keys.length; i += 100) {
    const batch = await api.post("/api/preview-urls", { keys: keys.slice(i, i + 100) });
    Object.assign(urls, batch.urls);
  }
  return urls;
}

function breadcrumb(prefix) {
  const parts = prefix.replace(/\/$/, "").split("/");
  let acc = "";
  const links = parts.map((part) => {
    acc += part + "/";
    return `<a href="#/browse?prefix=${encodeURIComponent(acc)}">${esc(part)}</a>`;
  });
  return links.join(" / ");
}

export async function renderBrowse(app, params, append = false) {
  const prefix = params.get("prefix") || "";
  if (!append || !state || state.prefix !== prefix) resetState(prefix);

  if (!append) app.innerHTML = `<p class="muted">Loading ${esc(prefix || "photos/")}…</p>`;

  let page;
  try {
    const qs = new URLSearchParams();
    if (prefix) qs.set("prefix", prefix);
    if (state.nextToken) qs.set("token", state.nextToken);
    page = await api.get(`/api/browse?${qs}`);
  } catch (err) {
    app.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    return;
  }

  const fresh = page.photos.filter((p) => !state.seen.has(p.key));
  fresh.forEach((p) => state.seen.add(p.key));
  state.photos.push(...fresh);
  state.nextToken = page.next_token || null;

  const [urls, playlists] = await Promise.all([
    previewUrls(fresh.map((p) => p.key)),
    api.get("/api/playlists").then((r) => r.playlists),
  ]);
  Object.assign(state.urls, urls);

  app.innerHTML = `
    <div class="browse-head">
      <div class="crumbs">${breadcrumb(page.prefix)}</div>
      <div class="add-bar">
        <span id="sel-count" class="muted"></span>
        <select id="playlist-pick">
          <option value="">Add selected to…</option>
          ${playlists.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}
        </select>
      </div>
    </div>
    ${page.folders.length ? `<div class="folders">${page.folders
      .map((f) => `<a class="folder" href="#/browse?prefix=${encodeURIComponent(f)}">📁 ${esc(basename(f.replace(/\/$/, "")))}</a>`)
      .join("")}</div>` : ""}
    <div class="grid" id="photo-grid"></div>
    ${state.nextToken ? `<button id="load-more">Load more</button>` : ""}
    ${!page.folders.length && !state.photos.length ? `<p class="muted">No photos here.</p>` : ""}
  `;

  const grid = document.getElementById("photo-grid");
  for (const photo of state.photos) {
    const card = document.createElement("label");
    card.className = "card" + (state.selected.has(photo.key) ? " selected" : "");
    card.innerHTML = `
      <input type="checkbox" ${state.selected.has(photo.key) ? "checked" : ""}>
      <img loading="lazy" src="${state.urls[photo.key] || ""}" alt="${esc(basename(photo.key))}">
      <span class="caption">${esc(basename(photo.key))}</span>
    `;
    card.querySelector("input").addEventListener("change", (e) => {
      e.target.checked ? state.selected.add(photo.key) : state.selected.delete(photo.key);
      card.classList.toggle("selected", e.target.checked);
      updateSelCount();
    });
    grid.appendChild(card);
  }

  function updateSelCount() {
    // The user may navigate away while an async handler is mid-flight; never
    // assume this view's DOM still exists.
    const el = document.getElementById("sel-count");
    if (el) el.textContent = state.selected.size ? `${state.selected.size} selected` : "";
  }
  updateSelCount();

  document.getElementById("load-more")?.addEventListener("click", () => renderBrowse(app, params, true));

  document.getElementById("playlist-pick").addEventListener("change", async (e) => {
    const pid = e.target.value;
    e.target.value = "";
    if (!pid || !state.selected.size) {
      if (!state.selected.size) toast("Select some photos first", true);
      return;
    }
    try {
      const playlist = await api.get(`/api/playlists/${pid}`);
      const existing = new Set(playlist.items);
      const additions = [...state.selected].filter((k) => !existing.has(k));
      if (additions.length) {
        await api.put(`/api/playlists/${pid}`, { items: [...playlist.items, ...additions] });
      }
      state.selected.clear();
      updateSelCount();
      document.querySelectorAll(".card input").forEach((cb) => (cb.checked = false));
      document.querySelectorAll(".card").forEach((c) => c.classList.remove("selected"));
      toast(additions.length
        ? `Added ${additions.length} photo(s) to "${playlist.name}"`
        : `Already in "${playlist.name}" — nothing added`);
    } catch (err) {
      toast(err.message, true);
    }
  });
}
