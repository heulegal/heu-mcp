"""Object storage for the remote server's bulk jobs.

Production uses an S3-compatible bucket (Tigris on Fly.io, pay-per-use): set
``BUCKET_NAME`` plus the usual ``AWS_*`` variables, which ``fly storage create``
does automatically. Without a bucket, files go to a local directory, which is
enough for development and tests.

All methods are synchronous; async callers run them in a worker thread.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import quote

from starlette.responses import FileResponse, RedirectResponse, Response


def _safe_key(key: str) -> str:
    parts = [p for p in key.split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("empty storage key")
    return "/".join(parts)


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _safe_key(key)

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def put_file(self, key: str, source: str | Path) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()

    def response(self, key: str, filename: str, ttl: int) -> Response:
        path = self._path(key)
        if not path.exists():
            return Response("File non più disponibile.", status_code=404)
        return FileResponse(path, filename=filename, headers={"Cache-Control": "no-store"})


class S3Storage:
    def __init__(self, bucket: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        # Endpoint and credentials come from AWS_ENDPOINT_URL_S3 / AWS_ACCESS_KEY_ID /
        # AWS_SECRET_ACCESS_KEY / AWS_REGION, set by `fly storage create`.
        self.client = boto3.client("s3", config=Config(signature_version="s3v4", retries={"max_attempts": 5}))

    def put_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=_safe_key(key), Body=data)

    def put_file(self, key: str, source: str | Path) -> None:
        self.client.upload_file(str(source), self.bucket, _safe_key(key))

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=_safe_key(key))["Body"].read()

    def delete_prefix(self, prefix: str) -> None:
        prefix = _safe_key(prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})

    def response(self, key: str, filename: str, ttl: int) -> Response:
        url = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": _safe_key(key),
                    "ResponseContentDisposition": _content_disposition(filename)},
            ExpiresIn=max(60, min(ttl, 3600)),
        )
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


def from_environment(local_root: str | Path):
    bucket = os.getenv("BUCKET_NAME")
    return S3Storage(bucket) if bucket else LocalStorage(local_root)
