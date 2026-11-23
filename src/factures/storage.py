"""Accès au stockage S3."""

from collections.abc import Iterator

import boto3
from botocore.config import Config

from .config import S3Target


def s3_client(target: S3Target):
    """Crée un client S3 connecté au bucket donné, avec sa propre clé."""
    return boto3.client(
        "s3",
        endpoint_url=target.endpoint_url,
        aws_access_key_id=target.access_key,
        aws_secret_access_key=target.secret_key,
        region_name=target.region,
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5}),
    )


def list_objects(target: S3Target, prefix: str = "") -> Iterator[dict]:
    """Liste tous les objets du bucket, page par page."""
    client = s3_client(target)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=target.bucket, Prefix=prefix):
        yield from page.get("Contents", [])
