async function apiPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

function log(msg) {
  const el = document.getElementById("log");
  el.textContent += msg + "\n";
}

async function uploadOne(file) {
  const api = window.PORTAL_API || {};
  const presignUrl = api.presignUpload;
  const completeUrl = api.completeUpload;
  if (!presignUrl || !completeUrl) {
    throw new Error("Upload API endpoints are not configured on this page.");
  }

  log(`Presigning: ${file.name}`);
  const presign = await apiPost(presignUrl, {
    filename: file.name,
    content_type: file.type || "image/jpeg",
  });

  const { key, presigned } = presign;
  const formData = new FormData();
  // presigned.fields includes required policy/signature + our key/content-type
  Object.entries(presigned.fields).forEach(([k, v]) => formData.append(k, v));
  formData.append("file", file);

  log(`Uploading to S3: ${key}`);
  const upRes = await fetch(presigned.url, { method: "POST", body: formData });
  if (!upRes.ok) {
    const t = await upRes.text();
    throw new Error(`S3 upload failed: ${upRes.status} ${t}`);
  }

  log(`Recording upload complete`);
  const done = await apiPost(completeUrl, {
    key,
    filename: file.name,
  });

  log(`Done: photo_id=${done.photo_id} published=${done.published} run_id=${done.publish_run_id}`);
}

document.getElementById("uploadBtn")?.addEventListener("click", async () => {
  const input = document.getElementById("file");
  const files = Array.from(input.files || []);
  if (!files.length) return;

  document.getElementById("log").textContent = "";
  try {
    for (const f of files) {
      await uploadOne(f);
    }
    log("All uploads complete.");
  } catch (e) {
    log("ERROR: " + (e?.message || e));
  }
});
