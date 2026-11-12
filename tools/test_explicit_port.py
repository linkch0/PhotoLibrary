"""An explicit --port must be honoured, not redirected to a running instance.

Scenario from real logs: a server is up on 8760, and a second launch asks for
--port 8850. It must bind 8850 rather than silently reporting 8760, because the
caller asked for a specific port (tests and scripts depend on that).

Also verifies that launching with no --port still reuses the running instance.

Usage: python tools/test_explicit_port.py <library>
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8760
EXPLICIT_PORT = 8850


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def start(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "app.launcher", *args, "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    lib = sys.argv[1]

    failures: list[str] = []

    print(f"starting instance A on the default port {DEFAULT_PORT}")
    a = start([lib])
    for _ in range(60):
        if health(DEFAULT_PORT):
            break
        if a.poll() is not None:
            print("A exited early:", a.stdout.read() if a.stdout else "")
            return 1
        time.sleep(0.5)
    print(f"  A healthy on {DEFAULT_PORT}: {health(DEFAULT_PORT)}")

    print(f"\nlaunching B with an explicit --port {EXPLICIT_PORT}")
    b = start([lib, "--port", str(EXPLICIT_PORT)])
    ok_b = False
    for _ in range(60):
        if health(EXPLICIT_PORT):
            ok_b = True
            break
        if b.poll() is not None:
            break
        time.sleep(0.5)
    print(f"  B healthy on {EXPLICIT_PORT}: {ok_b}")
    if not ok_b:
        out = b.stdout.read() if b.poll() is not None and b.stdout else "(still running)"
        print(f"  B output: {out}")
        failures.append(f"explicit port {EXPLICIT_PORT} was not honoured")

    print(f"  A still healthy on {DEFAULT_PORT}: {health(DEFAULT_PORT)}")
    if not health(DEFAULT_PORT):
        failures.append("starting B disrupted A")

    print("\nlaunching C with no --port (must reuse, exit 0 quickly)")
    c = subprocess.run(
        [sys.executable, "-m", "app.launcher", lib, "--no-browser"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=90,
    )
    print(f"  C exit {c.returncode}: {c.stdout.strip()}")
    if c.returncode != 0 or "already running" not in c.stdout:
        failures.append("no-port launch did not reuse the running instance")

    for p in (a, b):
        subprocess.run(["taskkill", "/F", "/PID", str(p.pid)], capture_output=True)
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()

    print(f"\nfailures: {len(failures)}")
    for f in failures:
        print("  ", f)
    print("RESULT:", "PASS" if not failures else "FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
