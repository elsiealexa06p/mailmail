"""Cloudflare R2 Manager.

Uploads assets (logos, catalogs) to R2 buckets via S3-compatible API.
Manages public access and custom domains via Cloudflare REST API.
"""
import os
import logging
from typing import List, Optional

try:
    import boto3
    from botocore.config import Config as BotoConfig
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger("bulk.r2")

CF_API = "https://api.cloudflare.com/client/v4"


class R2Manager:
    def __init__(self, account_id: str = "", api_token: str = "",
                 access_key_id: str = "", secret_access_key: str = "",
                 global_api_key: str = "", auth_email: str = ""):
        self._account_id = account_id
        self._api_token = api_token
        self._global_api_key = global_api_key
        self._auth_email = auth_email
        self._s3 = None

        if HAS_BOTO3 and account_id and access_key_id and secret_access_key:
            self._s3 = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
                config=BotoConfig(
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )

    @property
    def enabled(self) -> bool:
        return self._s3 is not None

    def list_buckets(self) -> List[str]:
        if not self._s3:
            return []
        try:
            resp = self._s3.list_buckets()
            return [b["Name"] for b in resp.get("Buckets", [])]
        except Exception as exc:
            logger.error("R2 list_buckets: %s", exc)
            return []

    def create_bucket(self, name: str) -> bool:
        if not self._s3:
            return False
        try:
            self._s3.create_bucket(Bucket=name)
            return True
        except Exception as exc:
            logger.error("R2 create_bucket: %s", exc)
            return False

    def upload_file(self, bucket: str, key: str, file_path: str,
                    content_type: str = "") -> Optional[str]:
        if not self._s3:
            return None
        try:
            extra = {}
            if content_type:
                extra["ContentType"] = content_type
            self._s3.upload_file(file_path, bucket, key, ExtraArgs=extra or None)
            return f"https://{bucket}.{self._account_id}.r2.dev/{key}"
        except Exception as exc:
            logger.error("R2 upload: %s", exc)
            return None

    def upload_bytes(self, bucket: str, key: str, data: bytes,
                     content_type: str = "application/octet-stream") -> Optional[str]:
        if not self._s3:
            return None
        try:
            self._s3.put_object(Bucket=bucket, Key=key, Body=data,
                                ContentType=content_type)
            return f"https://{bucket}.{self._account_id}.r2.dev/{key}"
        except Exception as exc:
            logger.error("R2 upload_bytes: %s", exc)
            return None

    def list_objects(self, bucket: str, prefix: str = "") -> List[dict]:
        if not self._s3:
            return []
        try:
            resp = self._s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
            return [{"key": o["Key"], "size": o["Size"]}
                    for o in resp.get("Contents", [])]
        except Exception as exc:
            logger.error("R2 list_objects: %s", exc)
            return []

    def delete_object(self, bucket: str, key: str) -> bool:
        if not self._s3:
            return False
        try:
            self._s3.delete_object(Bucket=bucket, Key=key)
            return True
        except Exception as exc:
            logger.error("R2 delete: %s", exc)
            return False

    # --- Cloudflare REST API for public access + custom domains ---

    def _cf_headers(self) -> dict:
        if self._global_api_key and self._auth_email:
            return {"X-Auth-Key": self._global_api_key,
                    "X-Auth-Email": self._auth_email,
                    "Content-Type": "application/json"}
        return {"Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json"}

    def _has_cf_auth(self) -> bool:
        return bool(self._api_token or (self._global_api_key and self._auth_email))

    def enable_public_access(self, bucket: str) -> bool:
        if not HAS_REQUESTS or not self._has_cf_auth():
            return False
        try:
            url = f"{CF_API}/accounts/{self._account_id}/r2/buckets/{bucket}/domains/managed"
            resp = _requests.put(url, headers=self._cf_headers(),
                                  json={"enabled": True})
            return resp.status_code == 200
        except Exception as exc:
            logger.error("R2 public access: %s", exc)
            return False

    def add_custom_domain(self, bucket: str, domain: str) -> bool:
        if not HAS_REQUESTS or not self._has_cf_auth():
            return False
        try:
            url = f"{CF_API}/accounts/{self._account_id}/r2/buckets/{bucket}/domains/custom"
            resp = _requests.post(url, headers=self._cf_headers(),
                                   json={"domain": domain})
            return resp.status_code in (200, 201)
        except Exception as exc:
            logger.error("R2 custom domain: %s", exc)
            return False

    def list_zones(self) -> List[dict]:
        if not HAS_REQUESTS or not self._has_cf_auth():
            return []
        try:
            resp = _requests.get(f"{CF_API}/zones", headers=self._cf_headers(),
                                  params={"per_page": 50})
            if resp.status_code == 200:
                return [{"id": z["id"], "name": z["name"]}
                        for z in resp.json().get("result", [])]
        except Exception as exc:
            logger.error("CF zones: %s", exc)
        return []
