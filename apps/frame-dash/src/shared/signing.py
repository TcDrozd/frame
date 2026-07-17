"""CloudFront URL signing for photo URLs.

Photo URLs in the manifest are CloudFront signed URLs (canned policy) built
with our own RSA key pair, so their lifetime is not capped by the Lambda
role session the way S3 presigned URLs are — TTLs of months are fine. The
private key lives in SSM Parameter Store (SecureString) and is cached for
the life of the execution environment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

_key_cache: dict[str, str] = {}


def get_private_key_pem(ssm_name: str) -> str:
    if ssm_name not in _key_cache:
        import boto3

        ssm = boto3.client("ssm")
        _key_cache[ssm_name] = ssm.get_parameter(Name=ssm_name, WithDecryption=True)[
            "Parameter"
        ]["Value"]
    return _key_cache[ssm_name]


def make_url_signer(domain: str, key_pair_id: str, private_key_pem: str, ttl_seconds: int):
    """Return (url_for, expires_at_epoch) for CloudFront signed URLs.

    All URLs from one publish share a single expiry so the manifest can
    advertise it as url_expires_at.
    """
    # CloudFront's signature scheme mandates SHA-1 with PKCS1v15.
    from botocore.signers import CloudFrontSigner
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)

    def rsa_signer(message: bytes) -> bytes:
        return key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    signer = CloudFrontSigner(key_pair_id, rsa_signer)
    expire_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    def url_for(s3_key: str) -> str:
        return signer.generate_presigned_url(
            f"https://{domain}/{quote(s3_key)}", date_less_than=expire_at
        )

    return url_for, int(expire_at.timestamp())
