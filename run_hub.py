#!/usr/bin/env python3
"""
DarkHub Launcher
Starts the FastAPI application and automatically opens http://127.0.0.1:8888 in the default browser.
"""

import os
import sys
import time
import socket
import webbrowser
import threading

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Add workspace directory to python path
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

DEFAULT_PORT = 8888
HOST = "127.0.0.1"


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0


def open_browser_delayed(url: str, delay: float = 1.2) -> None:
    time.sleep(delay)
    print(f"\n--> Abrindo navegador em: {url}\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception as exc:
        print(f"Aviso: Nao foi possivel abrir o navegador automaticamente: {exc}", flush=True)


def main() -> None:
    port = DEFAULT_PORT
    if is_port_in_use(port):
        print(f"[!] A porta {port} ja esta em uso. Tentando {port + 1}...", flush=True)
        port += 1

    url = f"http://{HOST}:{port}"

    print("=" * 65, flush=True)
    print(" [DarkHub] AI & Dev Command Center", flush=True)
    print(f" --> URL Local: {url}", flush=True)
    print(f" --> Swagger API Docs: {url}/docs", flush=True)
    print(" Pressione Ctrl+C para encerrar o servidor.", flush=True)
    print("=" * 65, flush=True)

    # Ensure daily model benchmark is updated (<1ms if already run today)
    try:
        from core.benchmarks.fetcher import ensure_daily_benchmark
        ensure_daily_benchmark()
    except Exception as exc:
        print(f"[!] Warning: Daily benchmark check skipped: {exc}", flush=True)

    # Launch browser in a background thread
    threading.Thread(target=open_browser_delayed, args=(url,), daemon=True).start()

    import uvicorn
    from hub.backend.main import app

    uvicorn.run(
        app,
        host=HOST,
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
