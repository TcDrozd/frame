export function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
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
