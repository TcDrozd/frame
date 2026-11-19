export function esc(text) {
  // Safe for text AND quoted HTML attributes. Never use for script/URL encoding.
  return String(text ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

export function basename(key) {
  return key.split("/").pop();
}

export function fmtBytes(n) {
  if (n == null) return "";
  if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  if (n > 1024) return Math.round(n / 1024) + " KB";
  return n + " B";
}
