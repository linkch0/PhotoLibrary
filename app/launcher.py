"""Launch the Photos library reader.

Runs the local server and opens the default browser. Designed to be started
by ``run.bat`` (double-click) or ``python -m app.launcher``.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import config


def _already_serving(port: int) -> bool:
    """True if a Photos reader is already answering on this port."""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return bool(json.loads(r.read() or b"{}").get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_file():
    return config.CACHE_ROOT / "server.port"


def _read_port_file() -> int | None:
    try:
        text = _port_file().read_text(encoding="utf-8").strip()
        return int(text) if text.isdigit() else None
    except (OSError, ValueError):
        return None


def _write_port_file(port: int) -> None:
    try:
        _port_file().parent.mkdir(parents=True, exist_ok=True)
        _port_file().write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def _clear_port_file(port: int | None = None) -> None:
    """Remove the port file, but only if it still points at our port."""
    try:
        if port is None or _read_port_file() == port:
            _port_file().unlink(missing_ok=True)
    except OSError:
        pass


def _running_instance(preferred: int) -> int | None:
    """Port of a Photos reader that is already up, whether or not it is on the
    preferred port.

    A first launch can land on a random port when the preferred one is busy;
    the port file is what lets a second launch find it instead of starting a
    rival copy on yet another port.

    A stale file left behind by a hard kill is harmless: every candidate is
    health-checked before use, so a dead port is simply skipped.
    """
    for port in (preferred, _read_port_file()):
        if port and _already_serving(port):
            return port
    return None


def free_port(preferred: int) -> int:
    for port in [preferred, 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def open_when_ready(port: int, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(f"http://127.0.0.1:{port}/")
                return
        time.sleep(0.2)


def discover_libraries() -> list[str]:
    """All real .photoslibrary bundles under the configured roots.

    Skips AppleDouble companions (``._Name.photoslibrary``) that macOS leaves
    on non-HFS volumes, and anything that is a file rather than a bundle.
    """
    found: list[str] = []
    for root in config.SEARCH_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            candidates = list(base.glob("*.photoslibrary"))
            candidates += list(base.glob("*/*.photoslibrary"))
        except OSError:
            continue
        for p in candidates:
            if p.name.startswith("._") or not p.is_dir():
                continue
            found.append(str(p))
    # Newest name first: these are dated (e.g. 2026.09.20-...).
    return sorted(dict.fromkeys(found), reverse=True)


def auto_library() -> str | None:
    """The single library to open automatically, if the choice is unambiguous.

    With several libraries on disk, opening one arbitrarily is worse than
    opening none - the user gets someone else's photos with no explanation.
    """
    libs = discover_libraries()
    if len(libs) == 1:
        return libs[0]
    if len(libs) > 1:
        print(f"Found {len(libs)} libraries; pick one in the app or pass a path:")
        for p in libs:
            print(f"  {p}")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse a macOS Photos library on Windows.")
    parser.add_argument("library", nargs="?", help="path to a .photoslibrary folder")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    config.ensure_dirs()

    # A second launch should focus the app that is already running - including
    # one that landed on a random port - rather than start an isolated copy.
    running = _running_instance(args.port)
    if running:
        print(f"A Photos reader is already running at http://{args.host}:{running}/")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{running}/")
        return 0

    port = free_port(args.port)
    if port != args.port:
        print(f"Port {args.port} is in use by another program; using {port} instead.")

    # Opening the library before the server starts means the first page load
    # already has data, which avoids a flash of the empty state.
    target = args.library or auto_library()
    if target:
        from .library import LibraryError, PhotosLibrary
        from . import server

        try:
            server._lib = PhotosLibrary(target)
            server._remember(str(server._lib.root))
            print(f"Opened {target}")
        except LibraryError as exc:
            print(f"Could not open library: {exc}", file=sys.stderr)
    else:
        # Seed the picker with everything we can find so the first screen is
        # useful instead of empty.
        from . import server

        for lib in discover_libraries():
            server._remember(lib)

    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()

    # Announce only once the socket is actually bound, so the message can never
    # claim a URL that failed to start. uvicorn sets `started` from inside
    # run(), so the server goes on a thread and this polls it.
    srv = uvicorn.Server(uvicorn.Config(server.app, host=args.host, port=port, log_level="warning"))
    runner = threading.Thread(target=srv.run, daemon=True)
    runner.start()

    for _ in range(300):
        if srv.started:
            _write_port_file(port)
            print(f"Photos library reader running at http://{args.host}:{port}/", flush=True)
            break
        if not runner.is_alive():
            print(f"Could not start the server on port {port}.", file=sys.stderr)
            return 1
        time.sleep(0.05)

    try:
        while runner.is_alive():
            runner.join(timeout=0.5)
    except KeyboardInterrupt:
        srv.should_exit = True
        runner.join(timeout=5)
    finally:
        _clear_port_file(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
