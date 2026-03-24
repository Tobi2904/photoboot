"""
s3_service.py — AWS S3 upload logic via boto3.

Handles:
- Uploading the final merged JPEG to a configured S3 bucket.
- Returning a public URL (or presigned URL) for download.
"""

import logging
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class S3UploadError(Exception):
    """Raised when the upload to S3 fails."""


def _get_client(
    aws_access_key_id: str,
    aws_secret_access_key: str,
    region: str,
) -> boto3.client:
    """Create a short-lived S3 client (no long-lived global state)."""
    return boto3.client(
        "s3",
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region,
    )


def upload_to_s3(
    file_path: str,
    bucket: str,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    region: str,
    use_presigned: bool = False,
    presigned_expiry: int = 3600,
) -> dict:
    """
    Upload a file to S3 and return its URL.

    Parameters
    ----------
    file_path             : Absolute path to the local file.
    bucket                : S3 bucket name.
    aws_access_key_id     : IAM access key.
    aws_secret_access_key : IAM secret.
    region                : AWS region (e.g. "ap-southeast-1").
    use_presigned         : If True, generate a presigned URL instead of
                            relying on public-read ACL.
    presigned_expiry      : Seconds until the presigned URL expires (default 1 h).

    Returns
    -------
    dict  {"s3_url": "<public or presigned URL>", "key": "<S3 object key>"}
    """
    path = Path(file_path)
    if not path.is_file():
        raise S3UploadError(f"File not found: {file_path}")

    s3_key = f"photobooth/{path.name}"
    client = _get_client(aws_access_key_id, aws_secret_access_key, region)

    try:
        extra_args = {"ContentType": "image/jpeg"}
        if not use_presigned:
            extra_args["ACL"] = "public-read"

        client.upload_file(
            Filename=str(path),
            Bucket=bucket,
            Key=s3_key,
            ExtraArgs=extra_args,
        )
        logger.info("Uploaded %s → s3://%s/%s", path.name, bucket, s3_key)
    except (BotoCoreError, ClientError) as exc:
        logger.error("S3 upload failed: %s", exc)
        raise S3UploadError(f"S3 upload failed: {exc}") from exc

    # Build URL
    if use_presigned:
        try:
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": s3_key},
                ExpiresIn=presigned_expiry,
            )
        except ClientError as exc:
            raise S3UploadError(f"Presigned URL generation failed: {exc}") from exc
    else:
        url = f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"

    return {"s3_url": url, "key": s3_key}
