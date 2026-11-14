import { ensureLogin, logout } from "./auth.js";
import { renderBrowse } from "./views/browse.js";
import { renderPlaylists, renderPlaylistEditor } from "./views/playlists.js";
import { renderStatus } from "./views/status.js";
import { confirmLeaveDraft } from "./draft.js";
import { esc } from "./util.js";

const app = document.getElementById("app");
let currentHash = null;
let restoringHash = false;

function route() {
  const hash = window.location.hash || "#/browse";
  if (restoringHash && hash === currentHash) { restoringHash = false; return; }
  if (!confirmLeaveDraft()) {
    restoringHash = true;
    window.location.hash = currentHash;
    return;
  }
  currentHash = hash;
  const [path, query] = hash.slice(2).split("?");
  const params = new URLSearchParams(query || "");

  document.querySelectorAll("nav a").forEach((a) => {
    a.classList.toggle("active", hash.startsWith(a.getAttribute("href")));
  });

  if (path === "browse") return renderBrowse(app, params);
  if (path === "playlists") return renderPlaylists(app);
  if (path.startsWith("playlist/")) return renderPlaylistEditor(app, decodeURIComponent(path.split("/")[1]));
  if (path === "status") return renderStatus(app);
  window.location.hash = "#/browse";
}

async function main() {
  await ensureLogin();
  const logoutBtn = document.getElementById("logout-btn");
  logoutBtn.hidden = false;
  logoutBtn.addEventListener("click", () => { if (confirmLeaveDraft()) logout(); });
  window.addEventListener("hashchange", () => Promise.resolve().then(route).catch(showError));
  await route();
}

function showError(err) {
  app.innerHTML = `<p class="error">Failed to load: ${esc(err.message)}</p>`;
}
main().catch(showError);
