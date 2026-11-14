#!/usr/bin/env python
"""CLI do mesmo preflight que `run_ticket` executa antes de PLAN."""

from core.orchestrator.manual_host_preflight import main


if __name__ == "__main__":
    raise SystemExit(main())
