"""Smoke-test run.bat: fresh venv path, server starts, UI serves, library opens.

Simulates what a user gets by double-clicking run.bat. Uses a spare port and
--no-browser so it can run unattended.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8791
LIB = sys.argv[1] if len(sys.argv) > 1 else None


def get(url: str, data: dict | None = None, timeout: float = 30):
    req = urllib.request.Request(url)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    if LIB is None:
        print(__doc__)
        print(f"usage: python {Path(__file__).name} <library>")
        return 2
    print("starting run.bat ...")
    proc = subprocess.Popen(
        ["cmd", "/c", "run.bat", LIB, "--port", str(PORT), "--no-browser"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base = f"http://127.0.0.1:{PORT}"
    ready = False
    for _ in range(120):
        try:
            status, body = get(base + "/api/health", timeout=2)
            if status == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.5)

    if not ready:
        proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        print("FAILED to start\n", out[-3000:])
        return 1

    print("server is up")
    status, body = get(base + "/api/library")
    info = json.loads(body)
    print(f"  library: {info['name']} ({info['stats']['visible']} items)")

    status, body = get(base + "/")
    html = body.decode("utf-8", "replace")
    print(f"  index.html: {status}, {len(body)} bytes, has app.js: {'app.js' in html}")

    status, body = get(base + "/api/assets?limit=2")
    items = json.loads(body)["items"]
    print(f"  assets: {len(items)} returned")

    status, body = get(base + f"/api/thumb/{items[0]['id']}?size=s")
    print(f"  thumbnail: {status}, {len(body)} bytes")

    # Shut down the whole tree (cmd -> python -> uvicorn).
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True)
    time.sleep(1)
    print("\nRESULT: run.bat OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
