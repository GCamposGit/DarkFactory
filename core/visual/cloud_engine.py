"""
Cloud & Multimodal Frontier Image Generation Engine.
Interfaces with:
1. OpenRouter Image / Multimodal APIs (google/gemini-2.5-flash-image, flux-1-schnell)
2. OpenAI DALL-E 3 API
3. Seamless Fallback to ProceduralVisualEngine ($0, 100% offline)
"""

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.paths import project_root
from typing import Optional

from PIL import Image

from core.execution.budget import ExecutionBudgetManager
from core.execution.contracts import AttemptOutcome, AttemptRecord
from core.execution.providers import get_openrouter_api_key
from core.visual.models import (
    ASPECT_DIMENSIONS,
    VisualAssetResult,
    VisualPromptSpec,
)
from core.visual.procedural_engine import ProceduralVisualEngine

logger = logging.getLogger("core.visual.cloud")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class CloudVisualEngine:
    """Dispatches visual asset generation to cloud providers with local fallback."""

    MAX_IMAGE_BYTES = 25 * 1024 * 1024

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        if output_dir is None:
            self.output_dir = project_root() / ".factory" / "visuals"
        else:
            self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.local_engine = ProceduralVisualEngine(output_dir=self.output_dir)

    @staticmethod
    def get_openrouter_key() -> Optional[str]:
        """Detect OpenRouter API key from environment variable or Windows Registry."""
        return get_openrouter_api_key()

    @staticmethod
    def get_openai_key() -> Optional[str]:
        """Detect OpenAI API key from environment variable or Windows Registry."""
        key = os.environ.get("OPENAI_API_KEY")
        if not key and sys.platform.startswith("win"):
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                    key, _ = winreg.QueryValueEx(k, "OPENAI_API_KEY")
            except Exception:
                pass
        return key if key and key.strip() else None

    def _safe_download_image(self, url: str, destination: Path) -> None:
        """Safely downloads an image with strict MAX_IMAGE_BYTES containment."""
        req = urllib.request.Request(url, headers={"User-Agent": "DarkFac-Visual-Studio/1.0"})
        total_bytes = 0
        temp_path = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(temp_path, "wb") as f:
                content_len = resp.headers.get("Content-Length")
                if content_len is not None:
                    try:
                        if int(content_len) > self.MAX_IMAGE_BYTES:
                            raise ValueError(
                                f"Content-Length {content_len} exceeds maximum safe limit of {self.MAX_IMAGE_BYTES} bytes"
                            )
                    except (ValueError, TypeError) as exc:
                        if isinstance(exc, ValueError) and "exceeds maximum" in str(exc):
                            raise

                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > self.MAX_IMAGE_BYTES:
                        raise ValueError(
                            f"Download stream exceeded maximum safe limit of {self.MAX_IMAGE_BYTES} bytes"
                        )
                    f.write(chunk)
            temp_path.replace(destination)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    def generate(
        self,
        spec: VisualPromptSpec,
        budget_manager: Optional[ExecutionBudgetManager] = None,
        budget_id: Optional[str] = None,
    ) -> VisualAssetResult:
        """Attempts cloud generation if keys exist and not offline; otherwise falls back to procedural."""
        attempt_id = f"visual-{spec.asset_type.value}-{uuid.uuid4().hex[:8]}"
        reservation = None
        if budget_manager is not None and budget_id is not None:
            reservation = budget_manager.reserve(
                budget_id=budget_id,
                attempt_id=attempt_id,
                amount=0.05,
            )

        try:
            result = self._execute_generation(spec)
            if budget_manager is not None and reservation is not None:
                attempt_rec = AttemptRecord(
                    attempt_id=attempt_id,
                    input_artifact_hash=hashlib.sha256((spec.prompt or spec.title).encode("utf-8")).hexdigest(),
                    output_artifact_hash=hashlib.sha256(result.file_path.encode("utf-8")).hexdigest(),
                    mode="offline" if spec.offline else "live",
                    tokens=0,
                    measured_cost=result.cost_usd,
                    estimated_cost=result.cost_usd,
                    latency=result.generation_time_ms / 1000.0,
                    outcome=AttemptOutcome.SUCCEEDED,
                    timestamp=datetime.now(timezone.utc),
                )
                budget_manager.commit(reservation.reservation_id, attempt_rec)
            return result
        except Exception as exc:
            if budget_manager is not None and reservation is not None:
                budget_manager.release(reservation.reservation_id, reason=str(exc))
            raise

    def _execute_generation(self, spec: VisualPromptSpec) -> VisualAssetResult:
        """Internal generation router between DALL-E 3, OpenRouter, and procedural engine."""
        if spec.offline:
            return self.local_engine.render(spec)

        openai_key = self.get_openai_key()
        openrouter_key = self.get_openrouter_key()
        cloud_errors = []
        original_provider_requested = None

        # Try DALL-E 3 if OpenAI key available
        if openai_key and (spec.model_override == "dall-e-3" or not openrouter_key):
            original_provider_requested = "openai_dalle3"
            try:
                return self._generate_dalle3(spec, openai_key)
            except Exception as exc:
                logger.warning("DALL-E 3 generation failed, trying fallback: %s", exc)
                cloud_errors.append(f"dall-e-3: {exc}")

        # Try OpenRouter if key available
        if openrouter_key:
            if original_provider_requested is None:
                original_provider_requested = "openrouter"
            try:
                return self._generate_openrouter_image(spec, openrouter_key)
            except Exception as exc:
                logger.warning("OpenRouter image generation failed, trying fallback: %s", exc)
                cloud_errors.append(f"openrouter: {exc}")

        if spec.strict_provider:
            if not openai_key and not openrouter_key:
                raise RuntimeError("strict cloud generation requires an OpenRouter or OpenAI key")
            detail = "; ".join(cloud_errors) or "requested cloud provider was not attempted"
            raise RuntimeError(f"strict cloud generation failed: {detail}")

        # High-aesthetic local procedural fallback
        result = self.local_engine.render(spec)
        if cloud_errors:
            result.fallback_occurred = True
            result.original_provider_requested = original_provider_requested or "cloud"
            result.fallback_reason = "; ".join(cloud_errors)
        return result

    def _generate_dalle3(self, spec: VisualPromptSpec, api_key: str) -> VisualAssetResult:
        start_time = time.time()
        size = "1792x1024" if "16:9" in spec.aspect_ratio.value else "1024x1024"

        payload = {
            "model": "dall-e-3",
            "prompt": spec.prompt or f"Technical artwork of {spec.title}",
            "n": 1,
            "size": size,
            "quality": "standard",
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/images/generations",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            img_url = data["data"][0]["url"]

        # Download and save with containment
        asset_id = f"vis_dalle_{int(start_time * 1000) % 1000000:06d}"
        file_path = self.output_dir / f"{asset_id}.png"
        self._safe_download_image(img_url, file_path)

        w, h = (1792, 1024) if "16:9" in spec.aspect_ratio.value else (1024, 1024)
        elapsed_ms = int((time.time() - start_time) * 1000)

        return VisualAssetResult(
            asset_id=asset_id,
            title=spec.title,
            asset_type=spec.asset_type,
            theme=spec.theme,
            aspect_ratio=spec.aspect_ratio,
            width=w,
            height=h,
            file_path=str(file_path),
            file_format="png",
            provider="openai_dalle3",
            model_used="dall-e-3",
            prompt_used=spec.prompt or spec.title,
            generation_time_ms=elapsed_ms,
            cost_usd=0.04,
        )

    def _generate_openrouter_image(self, spec: VisualPromptSpec, api_key: str) -> VisualAssetResult:
        """Call OpenRouter's dedicated Images API and persist the returned bytes."""
        start_time = time.time()
        model = spec.model_override or "google/gemini-3.1-flash-lite-image"

        payload = {
            "model": model,
            "prompt": spec.prompt or f"Generate a technical visual for {spec.title}",
            "aspect_ratio": spec.aspect_ratio.value,
        }
        if model.startswith("openai/"):
            payload["quality"] = "high" if spec.high_res else "auto"
        elif spec.high_res:
            payload["resolution"] = "2K"

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/images",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://darkfactory.local",
                "X-Title": "DarkFac Visual Studio",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        images = data.get("data", [])
        if not images or not isinstance(images[0], dict):
            raise ValueError("OpenRouter Images API returned no image records")
        image_record = images[0]
        encoded = image_record.get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("OpenRouter image record omitted b64_json")
        if len(encoded) > (self.MAX_IMAGE_BYTES * 4 // 3) + 8:
            raise ValueError("OpenRouter image payload exceeds the 25 MiB safety limit")

        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("OpenRouter returned invalid base64 image data") from exc
        if not image_bytes or len(image_bytes) > self.MAX_IMAGE_BYTES:
            raise ValueError("OpenRouter returned an empty or oversized image")

        media_type = str(image_record.get("media_type") or "image/png").lower()
        extension_by_media = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/svg+xml": "svg",
        }
        extension = extension_by_media.get(media_type)
        if extension is None:
            raise ValueError(f"unsupported OpenRouter image media type: {media_type}")

        width, height = ASPECT_DIMENSIONS[spec.aspect_ratio]
        if extension == "svg":
            if b"<svg" not in image_bytes[:1024].lower():
                raise ValueError("OpenRouter SVG payload has no svg root element")
        else:
            try:
                with Image.open(io.BytesIO(image_bytes)) as image:
                    image.verify()
                with Image.open(io.BytesIO(image_bytes)) as image:
                    width, height = image.size
            except Exception as exc:
                raise ValueError("OpenRouter returned invalid raster image bytes") from exc

        asset_id = f"vis_openrouter_{uuid.uuid4().hex[:10]}"
        file_path = self.output_dir / f"{asset_id}.{extension}"
        temp_path = file_path.with_suffix(f".{extension}.tmp")
        temp_path.write_bytes(image_bytes)
        temp_path.replace(file_path)

        usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
        cost_value = usage.get("cost", 0.0)
        try:
            cost_usd = float(cost_value or 0.0)
        except (TypeError, ValueError):
            cost_usd = 0.0

        return VisualAssetResult(
            asset_id=asset_id,
            title=spec.title,
            asset_type=spec.asset_type,
            theme=spec.theme,
            aspect_ratio=spec.aspect_ratio,
            width=width,
            height=height,
            file_path=str(file_path),
            file_format=extension,
            provider="openrouter",
            model_used=str(data.get("model") or model),
            prompt_used=payload["prompt"],
            generation_time_ms=int((time.time() - start_time) * 1000),
            cost_usd=cost_usd,
        )
