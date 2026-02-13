import hmac
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

PUBLISH_TOKEN = os.getenv("PUBLISH_TOKEN", "dev-token")
PUBLISHER_PORT = int(os.getenv("PUBLISHER_PORT", "5000"))

app = Flask(__name__)

# Path to your publisher script
PUBLISHER_SCRIPT = str(
    Path.home() / "frame/apps/portal/tools/publish_manifest.py"
)


def require_token(req):
    # Expect: Authorization: Bearer <token>
    auth = req.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth else ""
    return PUBLISH_TOKEN and hmac.compare_digest(token, PUBLISH_TOKEN)


@app.route("/publish", methods=["GET", "POST"])
def publish_manifest():
    """
    Endpoint to trigger photo frame manifest generation.

    Optional parameters (with defaults):
    - bucket: trevor-shared-photo-stream
    - region: us-east-1
    - photos_prefix: photos/
    - limit: 50
    - advance: 20
    - shuffle: daily
    - mode: sync
    - slide_seconds: 1380
    - expires: 43200
    """

    # if not require_token(request):
        # return jsonify({"error": "Unauthorized"}), 401

    # Get parameters from either GET query params or POST JSON body
    if request.method == "POST":
        params = request.get_json() or {}
    else:
        params = request.args.to_dict()

    # Define defaults
    defaults = {
        "bucket": "trevor-shared-photo-stream",
        "region": "us-east-1",
        "photos_prefix": "photos/",
        "limit": 50,
        "advance": 20,
        "shuffle": "daily",
        "mode": "sync",
        "slide_seconds": 1380,
        "expires": 43200,
        "mix": "collection",
    }

    # Merge params with defaults
    config = {**defaults, **params}

    # Build command
    cmd = [
        "/home/tcd/frame/apps/portal//venv/bin/python3",
        PUBLISHER_SCRIPT,
        "--bucket",
        str(config["bucket"]),
        "--region",
        str(config["region"]),
        "--photos-prefix",
        str(config["photos_prefix"]),
        "--limit",
        str(config["limit"]),
        "--advance",
        str(config["advance"]),
        "--shuffle",
        str(config["shuffle"]),
        "--mode",
        str(config["mode"]),
        "--slide-seconds",
        str(config["slide_seconds"]),
        "--expires",
        str(config["expires"]),
        "--mix",
        str(config["mix"]),
    ]

    try:
        # Run the publisher script
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        return jsonify(
            {
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "config": config,
            }
        ), 200 if result.returncode == 0 else 500

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Script execution timed out"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    script_exists = os.path.isfile(PUBLISHER_SCRIPT)
    return jsonify(
        {
            "status": "healthy" if script_exists else "degraded",
            "script_path": PUBLISHER_SCRIPT,
            "script_exists": script_exists,
        }
    )


if __name__ == "__main__":
    # Run on all interfaces, port 5000
    app.run(host="0.0.0.0", port=PUBLISHER_PORT, debug=False)

