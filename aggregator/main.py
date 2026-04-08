"""
Aggregator service: fans out to all shard log-readers, merges results.

ENV:
  SHARDS           number of shards (default 2)
  NAMESPACE        k8s namespace of vector pods (default ea-tapinfra)
  LOG_READER_PORT  port of log-reader sidecar (default 8080)
  PORT             port this service listens on (default 8090)
"""

import os
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

SHARDS = int(os.getenv("SHARDS", "2"))
NAMESPACE = os.getenv("NAMESPACE", "ea-tapinfra")
LOG_READER_PORT = os.getenv("LOG_READER_PORT", "8080")
PORT = int(os.getenv("PORT", "8090"))

# Cache: { date_str: { toolid: [shard_index, ...] } }
# Only past dates are cached (today is always re-scanned).
_cache: dict[str, dict[str, list[int]]] = {}


def _today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _shard_index_url(shard: int) -> str:
    return (
        f"http://vector-{shard}.{NAMESPACE}.svc.cluster.local"
        f":{LOG_READER_PORT}/logs/shard/index"
    )


def _shard_logs_url(shard: int) -> str:
    return (
        f"http://vector-{shard}.{NAMESPACE}.svc.cluster.local"
        f":{LOG_READER_PORT}/logs"
    )


def _query_shard_index(shard: int, date: str) -> dict:
    """Fetch toolid list from one shard. Returns {} on error."""
    try:
        r = requests.get(_shard_index_url(shard), params={"date": date}, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _scan_all_shards(date: str) -> dict[str, list[int]]:
    """Fan out to all shards and build { toolid: [shard, ...] } mapping."""
    mapping: dict[str, list[int]] = {}
    with ThreadPoolExecutor(max_workers=SHARDS) as ex:
        futures = {ex.submit(_query_shard_index, i, date): i for i in range(SHARDS)}
        for fut in as_completed(futures):
            shard_idx = futures[fut]
            data = fut.result()
            for toolid in data.get("toolids", []):
                mapping.setdefault(toolid, []).append(shard_idx)
    return mapping


def _get_index(date: str) -> dict[str, list[int]]:
    """Return cached mapping for past dates; always scan for today."""
    today = _today_utc()
    if date != today and date in _cache:
        return _cache[date]
    mapping = _scan_all_shards(date)
    if date != today:
        _cache[date] = mapping
    return mapping


def _fetch_content(shard: int, toolid: str, date: str) -> list[str]:
    """Fetch log lines from one shard using direct=true to skip re-routing."""
    try:
        r = requests.get(
            _shard_logs_url(shard),
            params={"toolid": toolid, "date": date, "direct": "true"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("lines", [])
    except Exception:
        pass
    return []


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return "", 200


@app.get("/logs")
def get_logs():
    toolid = request.args.get("toolid")
    if not toolid:
        return jsonify({"error": "toolid is required"}), 400

    date = request.args.get("date") or _today_utc()

    index = _get_index(date)
    shards = index.get(toolid, [])

    if not shards:
        return jsonify({"error": "log not found"}), 404

    # Fan out content reads to all shards that hold this toolid
    all_lines: list[str] = []
    with ThreadPoolExecutor(max_workers=len(shards)) as ex:
        futures = [ex.submit(_fetch_content, s, toolid, date) for s in shards]
        for fut in as_completed(futures):
            all_lines.extend(fut.result())

    return jsonify({
        "toolid": toolid,
        "date": date,
        "shards": shards,
        "lines": all_lines,
        "total_lines": len(all_lines),
    })


@app.get("/logs/all")
def get_all():
    date = request.args.get("date") or _today_utc()
    index = _get_index(date)
    toolids = sorted(index.keys())
    return jsonify({
        "date": date,
        "total_tools": len(toolids),
        "toolids": toolids,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
