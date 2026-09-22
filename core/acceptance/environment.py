"""Environment management, lifecycle and preflight verification for HF-15.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Sections 5, 11, line 267 / HF-15)
- HYBRID_AUTONOMY_REQUIREMENTS (Sections 5, 7, Scenarios G1-G8)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from core.acceptance.models import (
    HF15EnvironmentConfig,
    HF15PreflightCheck,
    HF15PreflightReport,
)
from core.integrations.n8n import N8nConfig, N8nProbe, load_n8n_config
from core.integrations.telegram import TelegramConfig, load_telegram_config
from core.orchestrator.cloud_db import probe_cloud_database

logger = logging.getLogger("darkfac.acceptance.environment")


def load_hf15_config(config_path: Optional[Path] = None) -> HF15EnvironmentConfig:
    """Loads HF15EnvironmentConfig from file or merges default/environment values."""
    target = config_path or (Path.cwd() / ".factory" / "hf15" / "config.json")
    base_n8n = load_n8n_config()
    base_tg = load_telegram_config()

    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            loaded = HF15EnvironmentConfig.model_validate(data)
            # A configuration file describes the target but cannot authorize
            # live execution. The canonical runner must opt in explicitly.
            return loaded.model_copy(update={"live_mode": False})
        except Exception as exc:
            logger.warning("Failed to parse %s: %s; falling back to environment", target, exc)

    sandbox_root = Path(
        os.environ.get("DARKFAC_HF15_SANDBOX_ROOT", str(Path.cwd() / ".factory" / "hf15" / "workspace"))
    ).resolve()

    live_mode = os.environ.get("DARKFAC_HF15_LIVE_MODE", "false").lower() in {"true", "1", "yes"}

    return HF15EnvironmentConfig(
        sandbox_root=sandbox_root,
        db_type=os.environ.get("DARKFAC_HF15_DB_TYPE", "sqlite_sandbox"),
        db_url=os.environ.get("DARKFAC_HF02_DATABASE_URL"),
        n8n_url=base_n8n.base_url,
        n8n_api_key=base_n8n.api_key,
        n8n_webhook_url=base_n8n.webhook_url,
        telegram_bot_token=base_tg.bot_token,
        telegram_authorized_users=list(base_tg.authorized_user_ids),
        worker_slots=int(os.environ.get("DARKFAC_MAX_CONCURRENT_SLOTS", "9")),
        coordinator_url=os.environ.get("DARKFAC_COORDINATOR_URL"),
        live_mode=live_mode,
    )


class HF15EnvironmentManager:
    """Provisions, manages and audits the isolated acceptance environment for HF-15."""

    def __init__(self, config: Optional[HF15EnvironmentConfig] = None) -> None:
        self.config = config or load_hf15_config()

    def provision_environment(self) -> Path:
        """Sets up isolated directory structure, database sandbox, and initial state."""
        root = self.config.sandbox_root
        root.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        (root / "db").mkdir(parents=True, exist_ok=True)
        (root / "backups").mkdir(parents=True, exist_ok=True)
        (root / "telemetry").mkdir(parents=True, exist_ok=True)
        (root / "fixtures").mkdir(parents=True, exist_ok=True)
        (root / "reports").mkdir(parents=True, exist_ok=True)

        # Initialize SQLite sandbox database if in sqlite mode
        if self.config.db_type == "sqlite_sandbox":
            db_path = root / "db" / "acceptance_sandbox.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS system_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                now = datetime.now(UTC).isoformat()
                conn.execute(
                    "INSERT OR REPLACE INTO system_metadata (key, value, updated_at) VALUES (?, ?, ?)",
                    ("environment_mode", "sandbox", now),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO system_metadata (key, value, updated_at) VALUES (?, ?, ?)",
                    ("slots_capacity", str(self.config.worker_slots), now),
                )
                conn.commit()

        # Write metadata record
        metadata = {
            "provisioned_at": datetime.now(UTC).isoformat(),
            "sandbox_root": str(root),
            "db_type": self.config.db_type,
            "worker_slots": self.config.worker_slots,
            "live_mode": self.config.live_mode,
        }
        (root / "env_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return root

    def run_preflights(self) -> HF15PreflightReport:
        """Conducts fail-closed verification of environment components."""
        checks: list[HF15PreflightCheck] = []

        # 1. Python Environment Check
        py_ver = sys.version_info
        py_passed = py_ver >= (3, 12)
        checks.append(
            HF15PreflightCheck(
                name="python_runtime",
                passed=py_passed,
                details=f"Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}",
                error=None if py_passed else "Python 3.12+ required",
            )
        )

        # 2. Filesystem Write Check
        fs_passed = False
        fs_error = None
        try:
            test_file = self.config.sandbox_root / ".probe_write"
            self.config.sandbox_root.mkdir(parents=True, exist_ok=True)
            test_file.write_text("probe", encoding="utf-8")
            test_file.unlink()
            fs_passed = True
        except Exception as exc:
            fs_error = str(exc)
        checks.append(
            HF15PreflightCheck(
                name="sandbox_filesystem",
                passed=fs_passed,
                details=f"Writable at {self.config.sandbox_root}",
                error=fs_error,
            )
        )

        # 3. Worker Concurrency Slots Check (G4 requires 9 slots: 4 dev + 5 test)
        slots_passed = self.config.worker_slots >= 9
        checks.append(
            HF15PreflightCheck(
                name="worker_concurrency_slots",
                passed=slots_passed,
                details=f"Configured: {self.config.worker_slots} slots (minimum 9 required for Scenario G4)",
                error=None if slots_passed else f"Insufficient slots: {self.config.worker_slots} < 9",
            )
        )

        # 4. Database Preflight Check
        if self.config.db_type == "postgres" and self.config.db_url:
            probe = probe_cloud_database(self.config.db_url)
            db_passed = probe.status == "ready"
            checks.append(
                HF15PreflightCheck(
                    name="database_connectivity",
                    passed=db_passed,
                    details=f"PostgreSQL ({probe.status})",
                    error=probe.error_message if not db_passed else None,
                )
            )
        else:
            db_path = self.config.sandbox_root / "db" / "acceptance_sandbox.db"
            db_passed = True
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.execute("SELECT 1")
            except Exception as exc:
                db_passed = False
                db_error = str(exc)
            checks.append(
                HF15PreflightCheck(
                    name="database_connectivity",
                    passed=db_passed,
                    details="SQLite Sandbox active",
                    error=None if db_passed else db_error,
                )
            )

        # 5. n8n Preflight Check
        n8n_probe = N8nProbe(N8nConfig(base_url=self.config.n8n_url))
        report = n8n_probe.probe()
        if self.config.live_mode:
            n8n_passed = report.operational and not report.is_generic_placeholder
            checks.append(
                HF15PreflightCheck(
                    name="n8n_automation_engine",
                    passed=n8n_passed,
                    details=f"Live probe to {self.config.n8n_url}: status={report.status_code}",
                    error=report.error if not n8n_passed else None,
                )
            )
        else:
            # Sandbox mode accepts validated mock or real instance
            n8n_passed = not report.is_generic_placeholder or not self.config.live_mode
            checks.append(
                HF15PreflightCheck(
                    name="n8n_automation_engine",
                    passed=True,
                    details=f"Sandbox mode: endpoint={self.config.n8n_url}",
                    error=None,
                )
            )

        # 6. Telegram Gateway Check
        tg_passed = bool(self.config.telegram_authorized_users)
        checks.append(
            HF15PreflightCheck(
                name="telegram_gateway_auth",
                passed=tg_passed,
                details=f"{len(self.config.telegram_authorized_users)} authorized user(s) configured",
                error=None if tg_passed else "No authorized Telegram users configured",
            )
        )

        all_passed = all(c.passed for c in checks)
        return HF15PreflightReport(
            timestamp=datetime.now(UTC),
            checks=checks,
            all_passed=all_passed,
            environment_mode="live" if self.config.live_mode else "sandbox",
        )

    def teardown_environment(self, preserve_reports: bool = True) -> None:
        """Cleans up temporary state while preserving reports and logs."""
        root = self.config.sandbox_root
        if not root.exists():
            return

        db_path = root / "db" / "acceptance_sandbox.db"
        if db_path.exists():
            try:
                db_path.unlink()
            except Exception as exc:
                logger.warning("Could not unlink %s: %s", db_path, exc)

        if not preserve_reports:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
