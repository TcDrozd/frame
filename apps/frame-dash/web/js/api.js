import { CONFIG } from "./config.js";
import { getAccessToken, tryRefresh, logout } from "./auth.js";

async function request(method, path, body) {
  const doFetch = () =>
    fetch(CONFIG.apiUrl + path, {
      method,
      headers: {
        Authorization: `Bearer ${getAccessToken()}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

  let resp = await doFetch();
  if (resp.status === 401 && (await tryRefresh())) resp = await doFetch();
  if (resp.status === 401) return logout();

  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  put: (path, body) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
};

export function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 4000);
}
