#!/usr/bin/env python3
"""
S3 Photo Selector - Admin tool for building manual photo lists/manifests.

Goals:
- Browse S3 under a fixed PHOTOS_PREFIX (no bucket-root listing required)
- Select photos and output a list (or manifest-ish JSON)
- Keep code modular (S3 ops separated from routes/template)
- Avoid fragile inline JS string escaping (DOM event listeners instead)
- Add /ping + JS console beacons for debugging "Loading..." issues

Run:
  python3 app.py

Then open:
  http://<host>:5001/
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

import boto3
from botocore.exceptions import ClientError
from flask import Flask, jsonify, render_template_string, request


# ----------------------------
# Config
# ----------------------------

@dataclass(frozen=True)
class Config:
    bucket_name: str = "trevor-shared-photo-stream"
    port: int = 5001
    photos_prefix: str = "photos/"          # MUST end with /
    presign_ttl_sec: int = 3600
    max_photos_returned: int = 250          # UI safety: don't presign 10k images at once


CFG = Config(
    bucket_name=os.getenv("BUCKET_NAME", "trevor-shared-photo-stream"),
    port=int(os.getenv("PORT", "5001")),
    photos_prefix=os.getenv("PHOTOS_PREFIX", "photos/"),
)


# ----------------------------
# S3 helpers
# ----------------------------

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")


def _normalize_requested_prefix(requested: str, photos_prefix: str) -> str:
    """
    UI sends prefix as:
      "" or "2025/12/"  (preferred, relative to photos_prefix)
    It might also send:
      "photos/2025/12/" (if you pass through S3 prefixes)
    We normalize to an S3 Prefix that ALWAYS starts with photos_prefix.
    """
    requested = (requested or "").lstrip("/")

    if requested.startswith(photos_prefix):
        requested = requested[len(photos_prefix):]

    # Ensure folder-ish prefixes end with "/" when non-empty
    if requested and not requested.endswith("/"):
        requested = requested + "/"

    return photos_prefix + requested


class S3Browser:
    def __init__(self, bucket: str, photos_prefix: str, presign_ttl: int) -> None:
        self.bucket = bucket
        self.photos_prefix = photos_prefix
        self.presign_ttl = presign_ttl
        self.s3 = boto3.client("s3")

    def list_folder(self, requested_prefix: str) -> Tuple[str, List[str], List[Dict[str, str]]]:
        """
        Returns:
          (effective_prefix, folders, photos)
        Where:
          folders = list of prefixes (strings)
          photos = list of dicts {key, name, url}
        """
        prefix = _normalize_requested_prefix(requested_prefix, self.photos_prefix)

        # Debug print (kept simple / grep-friendly)
        print(f"[s3] list_objects_v2 bucket={self.bucket} prefix={prefix}")

        resp = self.s3.list_objects_v2(
            Bucket=self.bucket,
            Prefix=prefix,
            Delimiter="/",
        )

        folders = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]

        photos: List[Dict[str, str]] = []
        for obj in resp.get("Contents", []):
            key = obj["Key"]

            # Skip folder-marker key
            if key == prefix:
                continue

            if not key.lower().endswith(IMAGE_EXTS):
                continue

            url = self.s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.presign_ttl,
            )

            photos.append(
                {
                    "key": key,
                    "name": key.split("/")[-1],
                    "url": url,
                }
            )

        return prefix, folders, photos


# ----------------------------
# Flask app
# ----------------------------

app = Flask(__name__)
browser = S3Browser(CFG.bucket_name, CFG.photos_prefix, presign_ttl=CFG.presign_ttl_sec)


# ----------------------------
# UI template
# ----------------------------

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>S3 Photo Selector</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif; background:#f5f5f5; padding:20px; }
    .container { max-width:1400px; margin:0 auto; background:white; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,.1); }
    .header { padding:20px 30px; border-bottom:1px solid #e0e0e0; background:#2c3e50; color:white; border-radius:8px 8px 0 0; }
    .header h1 { font-size:24px; font-weight:600; }
    .header p { margin-top:8px; opacity:.9; font-size:14px; }

    .controls { padding:20px 30px; background:#ecf0f1; border-bottom:1px solid #e0e0e0; display:flex; gap:15px; align-items:center; flex-wrap:wrap; }
    .breadcrumb { flex:1; min-width:200px; }
    .breadcrumb-path { display:flex; align-items:center; gap:5px; font-size:14px; color:#555; flex-wrap:wrap; }
    .breadcrumb-link { color:#3498db; cursor:pointer; text-decoration:none; }
    .breadcrumb-link:hover { text-decoration:underline; }

    .btn { padding:10px 20px; border:none; border-radius:4px; cursor:pointer; font-size:14px; font-weight:500; transition:background .2s; }
    .btn-primary { background:#3498db; color:white; }
    .btn-primary:hover { background:#2980b9; }
    .btn-success { background:#27ae60; color:white; }
    .btn-success:hover { background:#229954; }
    .btn-secondary { background:#95a5a6; color:white; }
    .btn-secondary:hover { background:#7f8c8d; }

    .selection-info { padding:10px 15px; background:#fff3cd; border:1px solid #ffc107; border-radius:4px; font-size:14px; color:#856404; display:none; }
    .content { padding:30px; }

    .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:20px; margin-bottom:30px; }
    .item { border:2px solid #e0e0e0; border-radius:6px; padding:10px; cursor:pointer; transition:all .2s; background:white; }
    .item:hover { border-color:#3498db; box-shadow:0 2px 8px rgba(52,152,219,.2); }
    .item.selected { border-color:#27ae60; background:#e8f8f0; }

    .folder { display:flex; align-items:center; gap:10px; padding:15px; }
    .folder-icon { font-size:32px; }
    .folder-name { font-weight:500; color:#2c3e50; }

    .photo { position:relative; }
    .photo-preview { width:100%; height:150px; object-fit:cover; border-radius:4px; background:#f0f0f0; }
    .photo-name { margin-top:8px; font-size:12px; color:#555; word-break:break-all; }

    .photo-checkbox { position:absolute; top:10px; right:10px; width:24px; height:24px; cursor:pointer; }

    .selected-list { margin-top:30px; padding:20px; background:#f8f9fa; border-radius:6px; display:none; }
    .selected-list.active { display:block; }
    .selected-list h3 { margin-bottom:15px; color:#2c3e50; }

    .selected-item { padding:8px 12px; background:white; border:1px solid #dee2e6; border-radius:4px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; font-size:13px; gap:12px; }
    .selected-item span.key { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .remove-btn { color:#e74c3c; cursor:pointer; font-weight:bold; padding:0 8px; }
    .remove-btn:hover { color:#c0392b; }

    .output { margin-top:20px; padding:20px; background:#2c3e50; border-radius:6px; display:none; }
    .output.active { display:block; }
    .output h3 { color:white; margin-bottom:15px; }
    .output pre { background:#1a252f; color:#2ecc71; padding:15px; border-radius:4px; overflow-x:auto; font-size:13px; line-height:1.5; }

    .empty-state { text-align:center; padding:60px 20px; color:#95a5a6; grid-column:1/-1; }
    .empty-state-icon { font-size:64px; margin-bottom:20px; }
    .loading { text-align:center; padding:40px; color:#95a5a6; grid-column:1/-1; }
    .debug { margin-top:10px; font-size:12px; opacity:.85; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>📸 S3 Photo Selector</h1>
      <p>Bucket: <strong>{{ bucket_name }}</strong></p>
      <div class="debug">
        Base prefix: <code>{{ photos_prefix }}</code>
        • API: <code>/list?prefix=...</code>
        • <code>/ping</code> should return pong
      </div>
    </div>

    <div class="controls">
      <div class="breadcrumb">
        <div class="breadcrumb-path" id="breadcrumb">
          <a class="breadcrumb-link" onclick="navigate('')">Home</a>
        </div>
      </div>

      <div class="selection-info" id="selectionInfo">
        <span id="selectionCount">0</span> photo(s) selected
      </div>

      <button class="btn btn-secondary" onclick="clearSelection()">Clear Selection</button>
      <button class="btn btn-success" onclick="generateList()">Generate List</button>
    </div>

    <div class="content">
      <div id="itemGrid" class="grid">
        <div class="loading">Loading...</div>
      </div>

      <div id="selectedList" class="selected-list">
        <h3>Selected Photos</h3>
        <div id="selectedItems"></div>
      </div>

      <div id="output" class="output">
        <h3>Generated List (keys)</h3>
        <pre id="outputText"></pre>
        <button class="btn btn-primary" onclick="copyToClipboard()" style="margin-top:15px;">Copy to Clipboard</button>
        <button class="btn btn-secondary" onclick="downloadText()" style="margin-top:15px;">Download .txt</button>
      </div>
    </div>
  </div>

  <script>
    // --- debug beacons ---
    console.log("JS loaded ✅");
    fetch("/ping").then(r => r.text()).then(t => console.log("Ping:", t)).catch(e => console.error("Ping failed", e));

    let currentPath = '';
    let selectedPhotos = new Set();

    function navigate(path) {
      currentPath = path || '';
      selectedPhotos.clear();
      updateUI();
      loadItems(currentPath);
    }

    function loadItems(path) {
      const grid = document.getElementById('itemGrid');
      grid.innerHTML = '<div class="loading">Loading...</div>';

      const url = '/list?prefix=' + encodeURIComponent(path);
      console.log("Loading:", url);

      fetch(url)
        .then(resp => {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(data => {
          displayItems(data);
          updateBreadcrumb(path);
        })
        .catch(err => {
          console.error("Load error:", err);
          grid.innerHTML =
            '<div class="empty-state"><div class="empty-state-icon">⚠️</div><p>Error loading items</p><p style="margin-top:10px;font-size:12px;">' +
            (err && err.message ? err.message : 'unknown error') + '</p></div>';
        });
    }

    function displayItems(data) {
      const grid = document.getElementById('itemGrid');
      grid.innerHTML = '';

      const folders = data.folders || [];
      const photos = data.photos || [];

      if (folders.length === 0 && photos.length === 0) {
        grid.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📁</div><p>No items found</p></div>';
        return;
      }

      // Folders
      folders.forEach(folderPrefix => {
        const div = document.createElement('div');
        div.className = 'item folder';

        // Prefer relative paths in UI, but backend tolerates either
        const relative = folderPrefix.replace(/^photos\\//, '');

        div.addEventListener('click', () => navigate(relative));
        div.innerHTML = `
          <div class="folder-icon">📁</div>
          <div class="folder-name">${folderPrefix.split('/').filter(x => x).pop()}</div>
        `;
        grid.appendChild(div);
      });

      // Photos (DOM-driven, no inline JS strings)
      photos.forEach(photo => {
        const div = document.createElement('div');
        div.className = 'item photo' + (selectedPhotos.has(photo.key) ? ' selected' : '');

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'photo-checkbox';
        checkbox.checked = selectedPhotos.has(photo.key);
        checkbox.addEventListener('change', (e) => {
          e.stopPropagation();
          toggleSelection(photo.key);
          // Update visuals in-place
          checkbox.checked = selectedPhotos.has(photo.key);
          div.classList.toggle('selected', selectedPhotos.has(photo.key));
        });

        const img = document.createElement('img');
        img.src = photo.url;
        img.className = 'photo-preview';
        img.alt = photo.name || '';

        const nameDiv = document.createElement('div');
        nameDiv.className = 'photo-name';
        nameDiv.textContent = photo.name || photo.key;

        div.addEventListener('click', () => {
          toggleSelection(photo.key);
          checkbox.checked = selectedPhotos.has(photo.key);
          div.classList.toggle('selected', selectedPhotos.has(photo.key));
        });

        div.appendChild(checkbox);
        div.appendChild(img);
        div.appendChild(nameDiv);
        grid.appendChild(div);
      });

      updateUI();
    }

    function toggleSelection(photoKey) {
      if (selectedPhotos.has(photoKey)) selectedPhotos.delete(photoKey);
      else selectedPhotos.add(photoKey);
      updateUI();
    }

    function clearSelection() {
      selectedPhotos.clear();
      updateUI();
      // do not reload items; just clear UI list/output
      // (checkbox visuals update when you click again)
    }

    function updateUI() {
      const count = selectedPhotos.size;
      const infoEl = document.getElementById('selectionInfo');
      const listEl = document.getElementById('selectedList');

      if (count > 0) {
        infoEl.style.display = 'block';
        listEl.classList.add('active');
        document.getElementById('selectionCount').textContent = count;

        const items = Array.from(selectedPhotos);
        document.getElementById('selectedItems').innerHTML = items.map(key => `
          <div class="selected-item">
            <span class="key" title="${escapeHtml(key)}">${escapeHtml(key)}</span>
            <span class="remove-btn" title="Remove">×</span>
          </div>
        `).join('');

        // Wire remove buttons after render
        Array.from(document.querySelectorAll('#selectedItems .selected-item')).forEach((row, idx) => {
          const key = items[idx];
          const btn = row.querySelector('.remove-btn');
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleSelection(key);
          });
        });

      } else {
        infoEl.style.display = 'none';
        listEl.classList.remove('active');
        document.getElementById('selectedItems').innerHTML = '';
      }

      document.getElementById('output').classList.remove('active');
    }

    function updateBreadcrumb(path) {
      const parts = path ? path.split('/').filter(x => x) : [];
      let html = '<a class="breadcrumb-link" onclick="navigate(\\'\\')">Home</a>';

      let p = '';
      parts.forEach(part => {
        p += part + '/';
        html += ' / <a class="breadcrumb-link" onclick="navigate(\\'' + escapeJsAttr(p) + '\\')">' + escapeHtml(part) + '</a>';
      });

      document.getElementById('breadcrumb').innerHTML = html;
    }

    function generateList() {
      if (selectedPhotos.size === 0) {
        alert('Please select at least one photo');
        return;
      }
      const keys = Array.from(selectedPhotos);
      const text = keys.join('\\n');

      document.getElementById('outputText').textContent = text;
      document.getElementById('output').classList.add('active');
      document.getElementById('output').scrollIntoView({ behavior: 'smooth' });
    }

    function copyToClipboard() {
      const text = document.getElementById('outputText').textContent;
      navigator.clipboard.writeText(text).then(() => alert('Copied to clipboard!'));
    }

    function downloadText() {
      const text = document.getElementById('outputText').textContent;
      const blob = new Blob([text], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'photo-keys-' + new Date().toISOString().split('T')[0] + '.txt';
      a.click();
      URL.revokeObjectURL(url);
    }

    function escapeHtml(s) {
      return String(s)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }
    function escapeJsAttr(s) {
      // For embedding into onclick="navigate('...')"
      return String(s).replaceAll('\\\\', '\\\\\\\\').replaceAll("'", "\\\\'");
    }

    // Initial load
    navigate('');
  </script>
</body>
</html>
"""


# ----------------------------
# Routes
# ----------------------------

@app.route("/ping")
def ping() -> Tuple[str, int]:
    return "pong\n", 200


@app.route("/")
def index():
    return render_template_string(
        HTML_TEMPLATE,
        bucket_name=CFG.bucket_name,
        photos_prefix=CFG.photos_prefix,
    )


@app.route("/list")
def list_items():
    requested_prefix = request.args.get("prefix", "")
    try:
        effective_prefix, folders, photos = browser.list_folder(requested_prefix)

        # Safety: don't presign an enormous set at once
        if len(photos) > CFG.max_photos_returned:
            photos = photos[: CFG.max_photos_returned]

        return jsonify(
            {
                "requested_prefix": requested_prefix,
                "effective_prefix": effective_prefix,
                "folders": folders,
                "photos": photos,
            }
        )

    except ClientError as e:
        return jsonify({"error": str(e)}), 500


# ----------------------------
# Entrypoint
# ----------------------------

if __name__ == "__main__":
    print(f"""
╔════════════════════════════════════════════════════════════════╗
║              S3 Photo Selector - Admin Tool                   ║
╚════════════════════════════════════════════════════════════════╝

📋 Configuration:
   • Bucket: {CFG.bucket_name}
   • Port: {CFG.port}
   • Photos prefix: {CFG.photos_prefix}

🚀 Starting server at:
   http://0.0.0.0:{CFG.port}

Press Ctrl+C to stop the server
""")

    app.run(host="0.0.0.0", port=CFG.port, debug=True)
