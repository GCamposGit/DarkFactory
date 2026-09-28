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
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def free_port_if_stale(port: int) -> bool:
    """Find and terminate any stale process holding the target port so new code is always served."""
    try:
        import subprocess
        output = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, stderr=subprocess.DEVNULL)
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and f":{port}" in parts[1] and parts[3] == "LISTENING":
                pid = int(parts[4])
                print(f"[*] Encerrando processo anterior (PID {pid}) que segurava a porta {port}...", flush=True)
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.2)
                return True
    except Exception as exc:
        print(f"[!] Aviso ao verificar porta {port}: {exc}", flush=True)
    return False


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
        freed = free_port_if_stale(port)
        if not freed or is_port_in_use(port):
            print(f"[!] A porta {port} segue em uso. Tentando {port + 1}...", flush=True)
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

    # Start Telegram background poller (USR-60 / HF-14)
    def start_telegram_listener(poll_interval: float = 2.5) -> None:
        try:
            from core.integrations.telegram import TelegramGateway, load_telegram_config
            cfg = load_telegram_config(role="ops")
            if not cfg.bot_token:
                return
            gw = TelegramGateway(cfg)
            print(" [*] Telegram Listener ativo para @darkfac_ops_bot (Entradas de Voz & Grill)", flush=True)
            while True:
                try:
                    gw.poll_updates(timeout=5)
                except Exception:
                    pass
                time.sleep(poll_interval)
        except Exception as exc:
            print(f"[!] Telegram Listener desativado: {exc}", flush=True)

    threading.Thread(target=start_telegram_listener, daemon=True, name="TelegramPoller").start()

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
