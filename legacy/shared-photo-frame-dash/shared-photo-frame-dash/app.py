import os
import math
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

try:
    # Optional: load .env if present
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

APP_HOST = os.getenv("HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", "8000"))

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "trevor-shared-photo-stream")
PHOTOS_PREFIX = os.getenv("PHOTOS_PREFIX", "photos/")

# If you want to run purely off env vars:
# export AWS_ACCESS_KEY_ID=...
# export AWS_SECRET_ACCESS_KEY=...
# export AWS_REGION=...
#
# Or rely on ~/.aws/credentials/profile via AWS_PROFILE
AWS_PROFILE = os.getenv("AWS_PROFILE")  # optional

# Presigned URL expiration (seconds)
PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "3600"))

# Hard safety limit for uploads (MB)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def s3_client():
    """
    Create an S3 client using either:
    - AWS_PROFILE (shared config/credentials), or
    - default credential chain (env vars, instance role, etc.)
    """
    cfg = Config(region_name=AWS_REGION, signature_version="s3v4")

    if AWS_PROFILE:
        session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    else:
        session = boto3.Session(region_name=AWS_REGION)

    return session.client("s3", config=cfg)


def human_bytes(n: int) -> str:
    if n <= 0:
        return "0 Bytes"
    units = ["Bytes", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(n, 1024)))
    i = min(i, len(units) - 1)
    return f"{round(n / (1024 ** i), 2)} {units[i]}"


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    # quick sanity check that creds/region/bucket are usable
    try:
        c = s3_client()
        c.head_bucket(Bucket=S3_BUCKET)
        return jsonify({"ok": True, "bucket": S3_BUCKET, "region": AWS_REGION})
    except ClientError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/photos")
def list_photos():
    """
    Returns:
      [
        { key, name, size, size_human, last_modified, url }
      ]
    """
    try:
        c = s3_client()

        # handle pagination, though you might not need it
        items = []
        token = None

        while True:
            params = {
                "Bucket": S3_BUCKET,
                "Prefix": PHOTOS_PREFIX,
                "MaxKeys": 1000,
            }
            if token:
                params["ContinuationToken"] = token

            resp = c.list_objects_v2(**params)
            for obj in resp.get("Contents", []):
                key = obj["Key"]
                if key == PHOTOS_PREFIX:
                    continue

                url = c.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={"Bucket": S3_BUCKET, "Key": key},
                    ExpiresIn=PRESIGN_EXPIRES,
                )

                name = key[len(PHOTOS_PREFIX):] if key.startswith(PHOTOS_PREFIX) else key
                last_modified = obj["LastModified"]
                if isinstance(last_modified, datetime):
                    last_modified = last_modified.astimezone(timezone.utc).isoformat()

                items.append(
                    {
                        "key": key,
                        "name": name,
                        "size": obj["Size"],
                        "size_human": human_bytes(obj["Size"]),
                        "last_modified": last_modified,
                        "url": url,
                    }
                )

            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break

        # newest first (optional)
        items.sort(key=lambda x: x.get("last_modified") or "", reverse=True)

        return jsonify({"ok": True, "count": len(items), "items": items})
    except ClientError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/upload")
def upload():
    """
    Multipart/form-data upload. Field name: "files" (multiple allowed)
    """
    if "files" not in request.files:
        return jsonify({"ok": False, "error": "No files field in request"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "No files provided"}), 400

    c = s3_client()
    results = []

    for f in files:
        if not f.filename:
            continue

        filename = secure_filename(f.filename)
        key = f"{PHOTOS_PREFIX}{filename}"

        try:
            content_type = f.mimetype or "application/octet-stream"

            c.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=f.stream,
                ContentType=content_type,
            )

            results.append({"name": filename, "key": key, "status": "ok"})
        except ClientError as e:
            results.append({"name": filename, "key": key, "status": "error", "error": str(e)})

    return jsonify({"ok": True, "results": results})


@app.delete("/api/photos")
def delete_photos():
    """
    JSON body:
      { "keys": ["photos/a.jpg", "photos/b.png"] }
    """
    data = request.get_json(silent=True) or {}
    keys = data.get("keys", [])

    if not isinstance(keys, list) or not keys:
        return jsonify({"ok": False, "error": "Provide JSON body: { keys: [...] }"}), 400

    # Safety: ensure deletes stay inside prefix unless you explicitly want broader
    for k in keys:
        if not isinstance(k, str) or not k.startswith(PHOTOS_PREFIX):
            return jsonify({"ok": False, "error": f"Refusing to delete key outside prefix: {k}"}), 400

    try:
        c = s3_client()

        # Batch delete (up to 1000 keys)
        chunks = [keys[i:i + 1000] for i in range(0, len(keys), 1000)]
        deleted = []
        errors = []

        for chunk in chunks:
            resp = c.delete_objects(
                Bucket=S3_BUCKET,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )
            deleted.extend([d["Key"] for d in resp.get("Deleted", [])])
            errors.extend(resp.get("Errors", []))

        return jsonify({"ok": True, "deleted": deleted, "errors": errors})
    except ClientError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT, debug=True)
