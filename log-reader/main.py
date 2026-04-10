"""
log-reader: sidecar HTTP server for querying logs from the local PVC.

Aggregator is the sole caller — it already knows which shard to call via
/logs/shard/index, so no proxy or routing logic is needed here.

ENV:
  MY_POD_NAME   pod name (downward API), e.g. "vector-0"
  LOG_DIR       log root directory (default /logs)
  PORT          listen port (default 8080)
"""

import datetime
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────

pod_name = os.getenv("MY_POD_NAME", "vector-0")
MY_SHARD = pod_name.rsplit("-", 1)[-1]   # "vector-0" → "0"

LOG_DIR = os.getenv("LOG_DIR", "/logs")
PORT    = int(os.getenv("PORT", "8080"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def read_local_file(toolid: str, date: str) -> list[str] | None:
    """Returns lines list, or None if file does not exist."""
    p = Path(LOG_DIR) / toolid / date / "app.log"
    if not p.exists():
        return None
    return p.read_text().splitlines()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return "", 200


@app.get("/logs/shard/index")
def shard_index():
    """Return toolids that have log files for the given date on this shard."""
    date = request.args.get("date") or today_utc()
    toolids = []
    try:
        for entry in sorted(Path(LOG_DIR).iterdir()):
            if entry.is_dir() and (entry / date / "app.log").exists():
                toolids.append(entry.name)
    except FileNotFoundError:
        pass
    return jsonify({"shard": MY_SHARD, "date": date, "toolids": toolids})


@app.get("/logs")
def get_logs():
    """Return log lines for toolid/date from this shard's local PVC."""
    toolid = request.args.get("toolid")
    if not toolid:
        return jsonify({"error": "toolid is required"}), 400

    date = request.args.get("date") or today_utc()

    lines = read_local_file(toolid, date)
    if lines is None:
        return jsonify({"error": "log not found"}), 404

    return jsonify({
        "toolid":      toolid,
        "date":        date,
        "shard":       MY_SHARD,
        "lines":       lines,
        "total_lines": len(lines),
    })


if __name__ == "__main__":
    app.logger.info("log-reader starting: shard=%s port=%d", MY_SHARD, PORT)
    app.run(host="0.0.0.0", port=PORT)
