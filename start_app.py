"""Double-click launcher for the local vocabulary illustration app."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")
    if importlib.util.find_spec("PIL") is None:
        print("Installing required packages...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                        str(ROOT / "requirements.txt")], check=True)

    import app

    app.init_db()
    port = int(os.environ.get("SISAVOCA_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    with ThreadingHTTPServer(("127.0.0.1", port), app.Handler) as server:
        print(f"시사보카 삽화 제작기: {url}", flush=True)
        print("창을 닫거나 Ctrl+C를 누르면 서버가 종료됩니다.", flush=True)
        browser = threading.Timer(0.5, webbrowser.open, args=(url,))
        browser.daemon = True
        browser.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
