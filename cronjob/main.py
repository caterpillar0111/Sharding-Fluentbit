import io
import logging
import os
from datetime import date, timedelta

import requests
from minio import Minio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

AGGREGATOR_URL = os.environ["AGGREGATOR_URL"].rstrip("/")
MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS_KEY = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET_KEY = os.environ["MINIO_SECRET_KEY"]
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "logs")
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").lower() == "true"


def get_toolids(target_date: str) -> list[str]:
    resp = requests.get(f"{AGGREGATOR_URL}/logs/all", params={"date": target_date}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("toolids", [])


def get_log_lines(toolid: str, target_date: str) -> list[str]:
    resp = requests.get(
        f"{AGGREGATOR_URL}/logs",
        params={"toolid": toolid, "date": target_date},
        timeout=60,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json().get("lines", [])


def upload(client: Minio, target_date: str, toolid: str, lines: list[str]) -> None:
    # sort by timestamp prefix (first 23 chars: "YYYY-MM-DD HH:mm:ss.ffff")
    sorted_lines = sorted(lines, key=lambda l: l[:23])
    content = "\n".join(sorted_lines) + "\n"
    data = content.encode()
    object_name = f"logs/{target_date}/{toolid}/app.log"
    client.put_object(
        MINIO_BUCKET,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type="text/plain",
    )
    log.info("uploaded %s (%d lines)", object_name, len(sorted_lines))


def main() -> None:
    target_date = os.environ.get("DATE") or str(date.today() - timedelta(days=1))
    log.info("starting upload for date=%s", target_date)

    client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=MINIO_SECURE)

    toolids = get_toolids(target_date)
    log.info("found %d toolids", len(toolids))

    failed = []
    for toolid in toolids:
        try:
            lines = get_log_lines(toolid, target_date)
            if not lines:
                log.warning("no lines for toolid=%s date=%s, skipping", toolid, target_date)
                continue
            upload(client, target_date, toolid, lines)
        except Exception as e:
            log.error("failed toolid=%s: %s", toolid, e)
            failed.append(toolid)

    if failed:
        log.error("upload finished with %d failures: %s", len(failed), failed)
        raise SystemExit(1)

    log.info("upload complete, %d toolids uploaded", len(toolids) - len(failed))


if __name__ == "__main__":
    main()
