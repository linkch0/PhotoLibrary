"""Verify second-launch reuse works even when the first fell back to a random port.

This is the case the old code got wrong: instance A starts while the preferred
port is busy, lands on a random port, and instance B then starts a rival copy
on yet another port because it only ever checked the preferred port.

Usage: python tools/test_reuse.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFERRED = 8821
LIB = sys.argv[1] if len(sys.argv) > 1 else None


def get(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    if LIB is None:
        print(__doc__)
        print(f"usage: python {Path(__file__).name} <library>")
        return 2
    print(f"occupying preferred port {PREFERRED} with a non-Photos listener")
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", PREFERRED))
    blocker.listen(1)

    a = subprocess.Popen(
        [sys.executable, "-m", "app.launcher", LIB, "--port", str(PREFERRED), "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(9)
    if a.poll() is not None:
        out = a.stdout.read() if a.stdout else ""
        print("first instance exited unexpectedly:\n", out)
        blocker.close()
        return 1

    port_file = Path.home() / "AppData/Local/MacPhotosReader/server.port"
    actual = int(port_file.read_text().strip()) if port_file.exists() else None
    print(f"instance A fell back to port {actual} (port file: {port_file})")
    if actual is None:
        print("FAIL: no port file written")
        a.kill(); blocker.close(); return 1

    get(f"http://127.0.0.1:{actual}/api/health")
    print("instance A is answering")

    print("\nlaunching instance B with no --port (must reuse A, not start a rival)")
    b = subprocess.run(
        [sys.executable, "-m", "app.launcher", LIB, "--no-browser"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=90,
    )
    print(f"  exit code: {b.returncode}")
    print(f"  output: {b.stdout.strip()}")

    ok = b.returncode == 0 and str(actual) in b.stdout

    still = get(f"http://127.0.0.1:{actual}/api/health")
    print(f"  instance A still healthy: {still.get('ok')}")

    a.kill(); a.wait(timeout=10)
    blocker.close()
    time.sleep(1.5)
    stale = port_file.exists()
    print(f"  port file after hard kill: {'left behind' if stale else 'cleaned'}")

    # A stale file must not break the next launch.
    c = subprocess.Popen(
        [sys.executable, "-m", "app.launcher", LIB, "--port", str(PREFERRED), "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(9)
    started = c.poll() is None
    new_port = None
    if port_file.exists():
        try:
            new_port = int(port_file.read_text().strip())
        except ValueError:
            new_port = None
    healthy = False
    if new_port:
        try:
            healthy = bool(get(f"http://127.0.0.1:{new_port}/api/health").get("ok"))
        except Exception:
            healthy = False
    print(f"  relaunch after stale file: started={started} port={new_port} healthy={healthy}")
    c.kill(); c.wait(timeout=10)

    print("\nRESULT:", "PASS" if (ok and still.get("ok") and started and healthy) else "FAIL")
    return 0 if (ok and still.get("ok") and started and healthy) else 1


if __name__ == "__main__":
    raise SystemExit(main())
