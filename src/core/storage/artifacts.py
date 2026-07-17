"""S3 artifact store — JD snapshots, resumes, screenshots, field maps, sessions.

One bucket, one prefix per artifact kind. Keys are returned to callers to
persist in the tracking row (e.g. ``resume_s3_key``).
"""

from __future__ import annotations

import boto3

PREFIXES = ("jd", "resumes", "screenshots", "fieldmaps", "sessions")


class ArtifactStore:
    def __init__(self, bucket: str, *, region: str | None = None) -> None:
        self._bucket = bucket
        self._s3 = boto3.client("s3", region_name=region)

    def put(self, prefix: str, key: str, data: bytes, content_type: str) -> str:
        if prefix not in PREFIXES:
            raise ValueError(f"unknown artifact prefix {prefix!r}")
        full_key = f"{prefix}/{key}"
        self._s3.put_object(
            Bucket=self._bucket, Key=full_key, Body=data, ContentType=content_type
        )
        return full_key

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def presign(self, key: str, *, expires: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires,
        )
