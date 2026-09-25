"""Cloudflare R2 / S3-Compatible Storage Client for Dark Factory Backups (INFRA-08).

Governed by:
- INFRA-08: Automação de Backups 3-Camadas (R2 + On-Premise)
- AWS Signature Version 4 (SigV4) authenticated S3 calls using native httpx.
- Deterministic mock / local fallback support for offline test suites.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class R2ObjectInfo(BaseModel):
    """Metadata describing an object stored in R2 / S3 storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(default="")
    last_modified: datetime = Field(default_factory=lambda: datetime.now(UTC))


class R2StorageClient:
    """Client for Cloudflare R2 / S3-compatible storage with native AWS SigV4."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        bucket_name: str | None = None,
        region: str = "auto",
        *,
        is_mock: bool | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.endpoint_url = (endpoint_url or os.getenv("R2_ENDPOINT_URL", "")).rstrip("/")
        self.access_key_id = access_key_id or os.getenv("R2_ACCESS_KEY_ID", "")
        self.secret_access_key = secret_access_key or os.getenv("R2_SECRET_ACCESS_KEY", "")
        self.bucket_name = bucket_name or os.getenv("R2_BUCKET_NAME", "darkfac-backups")
        self.region = region or os.getenv("R2_REGION", "auto")

        # Automatically operate in mock mode if credentials are missing and not explicitly set
        if is_mock is None:
            self.is_mock = not (self.endpoint_url and self.access_key_id and self.secret_access_key)
        else:
            self.is_mock = is_mock

        self._mock_objects: dict[str, tuple[bytes, datetime]] = {}
        self._http_client = http_client or httpx.Client(timeout=30.0)

    @property
    def configured(self) -> bool:
        """Indicates if client has credentials for live remote R2 operation."""
        return bool(self.endpoint_url and self.access_key_id and self.secret_access_key)

    def _sign_request(
        self,
        method: str,
        uri: str,
        query: str,
        headers: dict[str, str],
        payload_hash: str,
        dt: datetime,
    ) -> dict[str, str]:
        """Calculates AWS SigV4 authorization headers."""
        date_stamp = dt.strftime("%Y%m%d")
        amz_date = dt.strftime("%Y%m%dT%H%M%SZ")

        signed_headers_map = {k.lower(): v.strip() for k, v in headers.items()}
        signed_headers_map["x-amz-date"] = amz_date
        signed_headers_map["x-amz-content-sha256"] = payload_hash

        sorted_header_keys = sorted(signed_headers_map.keys())
        canonical_headers = "".join(f"{k}:{signed_headers_map[k]}\n" for k in sorted_header_keys)
        signed_headers_str = ";".join(sorted_header_keys)

        canonical_request = (
            f"{method}\n"
            f"{uri}\n"
            f"{query}\n"
            f"{canonical_headers}\n"
            f"{signed_headers_str}\n"
            f"{payload_hash}"
        )

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = (
            f"{algorithm}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        # Key derivation
        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k_secret = f"AWS4{self.secret_access_key}".encode("utf-8")
        k_date = _sign(k_secret, date_stamp)
        k_region = _sign(k_date, self.region)
        k_service = _sign(k_region, "s3")
        k_signing = _sign(k_service, "aws4_request")

        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth_header = (
            f"{algorithm} "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers_str}, "
            f"Signature={signature}"
        )

        out_headers = dict(headers)
        out_headers["x-amz-date"] = amz_date
        out_headers["x-amz-content-sha256"] = payload_hash
        out_headers["Authorization"] = auth_header
        return out_headers

    def upload_file(self, local_path: Path | str, object_key: str) -> R2ObjectInfo:
        """Uploads a local file to Cloudflare R2 / S3 bucket."""
        path = Path(local_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Local file to upload not found: {path}")

        data = path.read_bytes()
        sha256_hash = hashlib.sha256(data).hexdigest()
        now = datetime.now(UTC)

        if self.is_mock:
            logger.info("Mock R2 upload: %s (%d bytes)", object_key, len(data))
            self._mock_objects[object_key] = (data, now)
            return R2ObjectInfo(
                key=object_key,
                size_bytes=len(data),
                sha256=sha256_hash,
                last_modified=now,
            )

        # Live R2 HTTP upload
        url = f"{self.endpoint_url}/{self.bucket_name}/{urllib.parse.quote(object_key)}"
        parsed = urllib.parse.urlparse(url)
        headers = {
            "Host": parsed.netloc,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        }
        signed_headers = self._sign_request("PUT", parsed.path, "", headers, sha256_hash, now)

        resp = self._http_client.put(url, headers=signed_headers, content=data)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Failed to upload {object_key} to R2: HTTP {resp.status_code} - {resp.text}"
            )

        return R2ObjectInfo(
            key=object_key,
            size_bytes=len(data),
            sha256=sha256_hash,
            last_modified=now,
        )

    def download_file(self, object_key: str, dest_path: Path | str) -> Path:
        """Downloads an object from R2 to the local destination path."""
        dst = Path(dest_path).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)

        if self.is_mock:
            if object_key not in self._mock_objects:
                raise FileNotFoundError(f"Object not found in mock R2: {object_key}")
            data, _ = self._mock_objects[object_key]
            dst.write_bytes(data)
            return dst

        # Live R2 HTTP download
        url = f"{self.endpoint_url}/{self.bucket_name}/{urllib.parse.quote(object_key)}"
        parsed = urllib.parse.urlparse(url)
        now = datetime.now(UTC)
        empty_hash = hashlib.sha256(b"").hexdigest()
        headers = {"Host": parsed.netloc}
        signed_headers = self._sign_request("GET", parsed.path, "", headers, empty_hash, now)

        resp = self._http_client.get(url, headers=signed_headers)
        if resp.status_code == 404:
            raise FileNotFoundError(f"Object not found in R2: {object_key}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to download {object_key} from R2: HTTP {resp.status_code} - {resp.text}"
            )

        dst.write_bytes(resp.content)
        return dst

    def list_objects(self, prefix: str = "") -> list[R2ObjectInfo]:
        """Lists objects in the bucket matching the given prefix."""
        if self.is_mock:
            results: list[R2ObjectInfo] = []
            for k, (data, dt) in self._mock_objects.items():
                if k.startswith(prefix):
                    results.append(
                        R2ObjectInfo(
                            key=k,
                            size_bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest(),
                            last_modified=dt,
                        )
                    )
            return sorted(results, key=lambda x: x.last_modified, reverse=True)

        # Live S3 ListObjectsV2
        query = f"list-type=2&prefix={urllib.parse.quote(prefix)}"
        url = f"{self.endpoint_url}/{self.bucket_name}?{query}"
        parsed = urllib.parse.urlparse(url)
        now = datetime.now(UTC)
        empty_hash = hashlib.sha256(b"").hexdigest()
        headers = {"Host": parsed.netloc}
        signed_headers = self._sign_request("GET", parsed.path, query, headers, empty_hash, now)

        resp = self._http_client.get(url, headers=signed_headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to list objects in R2: HTTP {resp.status_code} - {resp.text}")

        # Basic XML parsing of ListBucketResult / Contents
        import xml.etree.ElementTree as ET

        root = ET.fromstring(resp.content)
        results = []
        # Handle namespaces if present
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        for item in root.findall(f"{ns}Contents"):
            k_elem = item.find(f"{ns}Key")
            sz_elem = item.find(f"{ns}Size")
            mod_elem = item.find(f"{ns}LastModified")

            k = k_elem.text if k_elem is not None and k_elem.text else ""
            sz = int(sz_elem.text) if sz_elem is not None and sz_elem.text else 0
            mod_dt = now
            if mod_elem is not None and mod_elem.text:
                try:
                    mod_dt = datetime.fromisoformat(mod_elem.text.replace("Z", "+00:00"))
                except ValueError:
                    pass

            results.append(
                R2ObjectInfo(
                    key=k,
                    size_bytes=sz,
                    last_modified=mod_dt,
                )
            )

        return sorted(results, key=lambda x: x.last_modified, reverse=True)

    def delete_object(self, object_key: str) -> bool:
        """Deletes an object from the bucket."""
        if self.is_mock:
            if object_key in self._mock_objects:
                del self._mock_objects[object_key]
                return True
            return False

        # Live S3 DeleteObject
        url = f"{self.endpoint_url}/{self.bucket_name}/{urllib.parse.quote(object_key)}"
        parsed = urllib.parse.urlparse(url)
        now = datetime.now(UTC)
        empty_hash = hashlib.sha256(b"").hexdigest()
        headers = {"Host": parsed.netloc}
        signed_headers = self._sign_request("DELETE", parsed.path, "", headers, empty_hash, now)

        resp = self._http_client.delete(url, headers=signed_headers)
        return resp.status_code in (200, 204)

    def object_exists(self, object_key: str) -> bool:
        """Checks if an object exists in storage."""
        if self.is_mock:
            return object_key in self._mock_objects

        url = f"{self.endpoint_url}/{self.bucket_name}/{urllib.parse.quote(object_key)}"
        parsed = urllib.parse.urlparse(url)
        now = datetime.now(UTC)
        empty_hash = hashlib.sha256(b"").hexdigest()
        headers = {"Host": parsed.netloc}
        signed_headers = self._sign_request("HEAD", parsed.path, "", headers, empty_hash, now)

        resp = self._http_client.head(url, headers=signed_headers)
        return resp.status_code == 200


__all__ = [
    "R2ObjectInfo",
    "R2StorageClient",
]
