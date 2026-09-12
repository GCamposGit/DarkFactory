"""
Hardware and runtime environment introspector for model telemetry.
Provides fast, cached, non-blocking hardware metrics and execution mode detection.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from core.telemetry.models import ExecutionMode

logger = logging.getLogger(__name__)

_CACHED_ACCELERATOR: Optional[str] = None


def _detect_accelerator() -> str:
    """Detect presence of GPU/CUDA or MPS acceleration, falling back to CPU."""
    global _CACHED_ACCELERATOR
    if _CACHED_ACCELERATOR is not None:
        return _CACHED_ACCELERATOR

    # 1. Try PyTorch if available
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            _CACHED_ACCELERATOR = f"cuda:0 ({name})"
            return _CACHED_ACCELERATOR
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            _CACHED_ACCELERATOR = "mps (Apple Silicon)"
            return _CACHED_ACCELERATOR
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("PyTorch acceleration check failed: %s", exc)

    # 2. Try nvidia-smi if on Windows/Linux
    if sys.platform in {"win32", "linux"}:
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore

            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=1.5,
                creationflags=creationflags,
            )
            if proc.returncode == 0:
                gpu_name = proc.stdout.strip().split("\n")[0].strip()
                if gpu_name:
                    _CACHED_ACCELERATOR = f"cuda:0 ({gpu_name})"
                    return _CACHED_ACCELERATOR
        except Exception:
            pass

    _CACHED_ACCELERATOR = f"cpu ({platform.machine() or 'standard'})"
    return _CACHED_ACCELERATOR


@dataclass(frozen=True)
class HardwareContext:
    device_name: str
    os_platform: str
    accelerator: str
    execution_mode: ExecutionMode


def get_hardware_context(
    explicit_mode: Optional[ExecutionMode | str] = None,
    device_override: Optional[str] = None,
) -> HardwareContext:
    """Introspects current hardware and execution context."""
    # Hostname
    device = device_override or os.environ.get("DARKFAC_DEVICE_NAME")
    if not device:
        try:
            device = socket.gethostname() or "unknown-device"
        except Exception:
            device = "unknown-device"

    # OS Platform
    try:
        os_name = f"{platform.system()} {platform.release()}".strip()
    except Exception:
        os_name = "Unknown-OS"

    # Accelerator
    accelerator = _detect_accelerator()

    # Execution Mode
    if explicit_mode:
        if isinstance(explicit_mode, str):
            try:
                mode = ExecutionMode(explicit_mode.lower())
            except ValueError:
                mode = ExecutionMode.HEADLESS
        else:
            mode = explicit_mode
    else:
        env_mode = os.environ.get("DARKFAC_EXECUTION_MODE")
        if env_mode:
            try:
                mode = ExecutionMode(env_mode.lower())
            except ValueError:
                mode = ExecutionMode.HEADLESS
        else:
            mode = ExecutionMode.HEADLESS

    return HardwareContext(
        device_name=device,
        os_platform=os_name,
        accelerator=accelerator,
        execution_mode=mode,
    )
