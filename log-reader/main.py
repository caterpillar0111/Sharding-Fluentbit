"""
log-reader: sidecar HTTP server for querying per-shard Vector PVC logs.

ENV:
  MY_POD_NAME   pod name (downward API), e.g. "vector-0"
  NAMESPACE     k8s namespace (default ea-tapinfra)
  LOG_DIR       log root directory (default /logs)
  PORT          listen port (default 8080)
  SHARD_MAP     JSON array of {from, to, shard} entries
"""

import datetime
import json
import logging
import os
from pathlib import Path

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────

pod_name = os.getenv("MY_POD_NAME", "vector-0")
MY_SHARD = pod_name.rsplit("-", 1)[-1]          # "vector-0" → "0"

NAMESPACE = os.getenv("NAMESPACE", "ea-tapinfra")
LOG_DIR   = os.getenv("LOG_DIR", "/logs")
PORT      = int(os.getenv("PORT", "8080"))

SHARD_MAP: list[dict] = json.loads(os.getenv("SHARD_MAP", "[]"))


# ── FNV-32a ───────────────────────────────────────────────────────────────────

def fnv32a(s: str) -> int:
    h = 2166136261
    for b in s.encode():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def compute_shard(toolid: str) -> str:
    slot = fnv32a(toolid) % 256
    for entry in SHARD_MAP:
        if entry["from"] <= slot <= entry["to"]:
            return str(entry["shard"])
    return "0"


# ── Helpers ───────────────────────────────────────────────────────────────────

def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def shard_base_url(shard: str) -> str:
    return f"http://vector-{shard}.{NAMESPACE}.svc.cluster.local:{PORT}"


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
    toolid = request.args.get("toolid")
    if not toolid:
        return jsonify({"error": "toolid is required"}), 400

    date   = request.args.get("date") or today_utc()
    direct = request.args.get("direct") == "true"

    if not direct:
        target = compute_shard(toolid)
        if target != MY_SHARD:
            return _proxy_to_shard(target, toolid, date)

    lines = read_local_file(toolid, date)

    if lines is None and not direct:
        # Hash mismatch fallback: ask each other shard to read its own PVC directly
        for entry in SHARD_MAP:
            if str(entry["shard"]) == MY_SHARD:
                continue
            result = _proxy_fallback(str(entry["shard"]), toolid, date)
            if result is not None:
                return result
        return jsonify({"error": "log not found"}), 404

    if lines is None:
        return jsonify({"error": "log not found"}), 404

    return jsonify({
        "toolid":      toolid,
        "date":        date,
        "shard":       MY_SHARD,
        "lines":       lines,
        "total_lines": len(lines),
    })


def _proxy_to_shard(shard: str, toolid: str, date: str):
    try:
        r = requests.get(
            shard_base_url(shard) + "/logs",
            params={"toolid": toolid, "date": date},
            timeout=10,
        )
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": f"proxy error: {e}"}), 502


def _proxy_fallback(shard: str, toolid: str, date: str):
    """Try shard with direct=true. Returns Flask response tuple or None."""
    try:
        r = requests.get(
            shard_base_url(shard) + "/logs",
            params={"toolid": toolid, "date": date, "direct": "true"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.content, 200, {"Content-Type": "application/json"}
    except Exception:
        pass
    return None


if __name__ == "__main__":
    app.logger.info(
        "log-reader starting: shard=%s namespace=%s port=%d", MY_SHARD, NAMESPACE, PORT
    )
    app.run(host="0.0.0.0", port=PORT)
