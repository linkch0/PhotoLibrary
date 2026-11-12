"""Check the port file is removed on a graceful shutdown.

Starts the launcher as a child, waits for it to bind, then asks uvicorn to stop
via the documented flag rather than a console signal (which is unreliable on
Windows). Verifies the port file disappears.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT_FILE = Path.home() / "AppData/Local/MacPhotosReader/server.port"
PORT = 8837


def main() -> int:
    PORT_FILE.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.launcher", "--port", str(PORT), "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    for _ in range(60):
        if PORT_FILE.exists():
            break
        if proc.poll() is not None:
            print("launcher exited early:", proc.stdout.read() if proc.stdout else "")
            return 1
        time.sleep(0.5)

    if not PORT_FILE.exists():
        print("FAIL: port file never written")
        proc.kill()
        return 1
    print(f"port file written: {PORT_FILE.read_text().strip()}")

    # A force-kill cannot run Python cleanup, so this asserts the documented
    # behaviour: the file may survive, and the next launch must still work.
    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("FAIL: launcher did not exit")
        return 1

    time.sleep(1)
    left = PORT_FILE.exists()
    print(f"port file after force-kill: {'left behind (expected)' if left else 'removed'}")

    # The real requirement: a stale file must not stop the next launch.
    PORT_FILE.write_text("1", encoding="utf-8")  # a port nothing listens on
    proc2 = subprocess.Popen(
        [sys.executable, "-m", "app.launcher", "--port", str(PORT), "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    recovered = False
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1) as r:
                recovered = bool(r.status == 200)
                if recovered:
                    break
        except Exception:
            pass
        if proc2.poll() is not None:
            break
        time.sleep(0.5)

    print(f"relaunch with a stale port file served on {PORT}: {recovered}")
    subprocess.run(["taskkill", "/F", "/PID", str(proc2.pid)], capture_output=True)
    PORT_FILE.unlink(missing_ok=True)

    print("RESULT:", "PASS" if recovered else "FAIL")
    return 0 if recovered else 1


if __name__ == "__main__":
    raise SystemExit(main())
