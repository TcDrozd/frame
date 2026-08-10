import { ensureLogin, logout } from "./auth.js";
import { renderBrowse } from "./views/browse.js";
import { renderPlaylists, renderPlaylistEditor } from "./views/playlists.js";
import { renderStatus } from "./views/status.js";

const app = document.getElementById("app");

function route() {
  const hash = window.location.hash || "#/browse";
  const [path, query] = hash.slice(2).split("?");
  const params = new URLSearchParams(query || "");

  document.querySelectorAll("nav a").forEach((a) => {
    a.classList.toggle("active", hash.startsWith(a.getAttribute("href")));
  });

  if (path === "browse") return renderBrowse(app, params);
  if (path === "playlists") return renderPlaylists(app);
  if (path.startsWith("playlist/")) return renderPlaylistEditor(app, path.split("/")[1]);
  if (path === "status") return renderStatus(app);
  window.location.hash = "#/browse";
}

async function main() {
  await ensureLogin();
  const logoutBtn = document.getElementById("logout-btn");
  logoutBtn.hidden = false;
  logoutBtn.addEventListener("click", logout);
  window.addEventListener("hashchange", route);
  route();
}

main().catch((err) => {
  app.innerHTML = `<p class="error">Failed to start: ${err.message}</p>`;
});
