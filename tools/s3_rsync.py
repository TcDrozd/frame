#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import mimetypes
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError, ProfileNotFound


DEFAULT_CACHE = ".s3_rsync_cache.json"
DEFAULT_WORKERS = 4
HASH_CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_EXCLUDES = {".DS_Store", "Thumbs.db"}


@dataclass(frozen=True)
class LocalFile:
    path: Path
    rel_posix: str
    size: int
    mtime: int


@dataclass(frozen=True)
class S3Destination:
    bucket: str
    prefix: str

    def key_for(self, rel_posix: str) -> str:
        if not self.prefix:
            return rel_posix
        return f"{self.prefix}{rel_posix}"

    def uri_for_key(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"


@dataclass
class Summary:
    scanned: int = 0
    uploaded: int = 0
    uploaded_bytes: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: int = 0
    deleted: int = 0


class ShaCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._hash_index: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        entries = raw.get("entries", {})
        hash_index = raw.get("hash_index", {})
        if isinstance(entries, dict):
            self._entries = entries
        if isinstance(hash_index, dict):
            self._hash_index = hash_index

    def get_sha(self, file_path: Path, size: int, mtime: int) -> Optional[str]:
        key = str(file_path.resolve())
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            if entry.get("size") != size or entry.get("mtime") != mtime:
                return None
            sha = entry.get("sha256")
            return sha if isinstance(sha, str) else None

    def set_sha(
        self,
        file_path: Path,
        size: int,
        mtime: int,
        sha256: str,
        s3_uri: Optional[str] = None,
    ) -> None:
        key = str(file_path.resolve())
        with self._lock:
            entry: Dict[str, Any] = {"size": size, "mtime": mtime, "sha256": sha256}
            if s3_uri:
                entry["s3_uri"] = s3_uri
                self._hash_index[sha256] = s3_uri
            self._entries[key] = entry

    def get_canonical_uri(self, sha256: str) -> Optional[str]:
        with self._lock:
            value = self._hash_index.get(sha256)
            return value if isinstance(value, str) else None

    def set_canonical_uri(self, sha256: str, s3_uri: Optional[str]) -> None:
        with self._lock:
            if s3_uri:
                self._hash_index[sha256] = s3_uri
            else:
                self._hash_index.pop(sha256, None)

    def save(self) -> None:
        with self._lock:
            data = {"version": 1, "entries": self._entries, "hash_index": self._hash_index}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name, dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def parse_s3_uri(value: str) -> S3Destination:
    parsed = urlparse(value)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {value!r}")
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return S3Destination(bucket=bucket, prefix=prefix)


def to_rel_posix(path: Path, source: Path) -> str:
    rel = path.relative_to(source)
    return rel.as_posix()


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    units = ["KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f}{unit}"
    return f"{value:.1f}PB"


def should_include(
    rel_posix: str,
    include: Sequence[str],
    exclude: Sequence[str],
    skip_junk: bool,
) -> bool:
    name = Path(rel_posix).name
    if skip_junk and name in DEFAULT_EXCLUDES:
        if include and any(fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(name, pattern) for pattern in include):
            return True
        return False
    if include:
        if not any(fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(name, pattern) for pattern in include):
            return False
    if exclude and any(fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(name, pattern) for pattern in exclude):
        return False
    return True


def iter_local_files(
    source: Path,
    follow_symlinks: bool,
    include: Sequence[str],
    exclude: Sequence[str],
    skip_junk: bool,
) -> Iterable[LocalFile]:
    for root, _, files in os.walk(source, followlinks=follow_symlinks):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            try:
                st = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            rel = to_rel_posix(path, source)
            if should_include(rel, include=include, exclude=exclude, skip_junk=skip_junk):
                yield LocalFile(path=path, rel_posix=rel, size=st.st_size, mtime=int(st.st_mtime))


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def get_local_sha(cache: ShaCache, file_obj: LocalFile) -> str:
    cached = cache.get_sha(file_obj.path, file_obj.size, file_obj.mtime)
    if cached:
        return cached
    sha = compute_sha256(file_obj.path)
    cache.set_sha(file_obj.path, file_obj.size, file_obj.mtime, sha)
    return sha


def head_object_metadata(
    client: Any,
    bucket: str,
    key: str,
) -> Tuple[bool, Optional[Dict[str, str]]]:
    try:
        response = client.head_object(Bucket=bucket, Key=key)
        metadata = response.get("Metadata", {}) or {}
        return True, {str(k): str(v) for k, v in metadata.items()}
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False, None
        raise


def key_exists(client: Any, bucket: str, key: str) -> bool:
    exists, _ = head_object_metadata(client, bucket, key)
    return exists


def choose_upload_action(
    remote_exists: bool,
    remote_metadata: Optional[Dict[str, str]],
    file_obj: LocalFile,
    local_sha: Optional[str],
) -> Tuple[bool, str]:
    if not remote_exists:
        return True, "remote missing"
    metadata = remote_metadata or {}
    remote_sha = metadata.get("sha256")
    remote_size = metadata.get("size")
    remote_mtime = metadata.get("mtime")
    if remote_sha:
        if local_sha is None:
            raise ValueError("local_sha required when remote sha256 is present")
        if remote_sha == local_sha:
            return False, "remote sha256 match"
        return True, "remote sha256 mismatch"
    if remote_size is not None and remote_mtime is not None:
        if str(file_obj.size) == remote_size and str(file_obj.mtime) == remote_mtime:
            return False, "remote size+mtime match"
    return True, "remote metadata mismatch"


def content_type_for(path: Path) -> Optional[str]:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed


def upload_one(
    client: Any,
    file_obj: LocalFile,
    bucket: str,
    key: str,
    sha256: str,
    dry_run: bool,
) -> Tuple[bool, str]:
    if dry_run:
        return True, "dry-run"
    extra_args: Dict[str, Any] = {
        "Metadata": {
            "sha256": sha256,
            "size": str(file_obj.size),
            "mtime": str(file_obj.mtime),
        }
    }
    content_type = content_type_for(file_obj.path)
    if content_type:
        extra_args["ContentType"] = content_type
    client.upload_file(str(file_obj.path), bucket, key, ExtraArgs=extra_args)
    return True, "uploaded"


def maybe_delete_remote(
    client: Any,
    destination: S3Destination,
    local_keys: set[str],
    dry_run: bool,
    require_confirmation: bool,
) -> Tuple[int, int]:
    deleted = 0
    errors = 0
    continuation: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": destination.bucket, "Prefix": destination.prefix}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = client.list_objects_v2(**kwargs)
        for item in resp.get("Contents", []):
            key = item.get("Key")
            if not key:
                continue
            if key in local_keys:
                continue
            if dry_run:
                print(f"DELETE {destination.uri_for_key(key)} (dry-run)")
                deleted += 1
                continue
            if require_confirmation:
                prompt = f"Delete remote object {destination.uri_for_key(key)}? [y/N]: "
                answer = input(prompt).strip().lower()
                if answer not in {"y", "yes"}:
                    continue
            try:
                client.delete_object(Bucket=destination.bucket, Key=key)
                print(f"DELETE {destination.uri_for_key(key)}")
                deleted += 1
            except ClientError as exc:
                print(f"ERROR deleting {destination.uri_for_key(key)} ({exc})")
                errors += 1
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    return deleted, errors


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync a local directory tree to S3 with idempotent uploads.")
    parser.add_argument("--source", help="Local source directory.")
    parser.add_argument("--dest", help="Destination S3 URI (s3://bucket/prefix).")
    parser.add_argument("--profile", help="AWS profile name.")
    parser.add_argument("--region", help="AWS region.")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without uploading.")
    parser.add_argument("--delete", action="store_true", help="Delete remote objects missing locally.")
    parser.add_argument("--yes", action="store_true", help="Confirm dangerous operations non-interactively.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Concurrent upload workers (default: 4).")
    parser.add_argument("--cache", default=DEFAULT_CACHE, help="Cache file path (default: .s3_rsync_cache.json).")
    parser.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks during directory walk.")
    parser.add_argument("--include", action="append", default=[], help="Include glob pattern (repeatable).")
    parser.add_argument("--exclude", action="append", default=[], help="Exclude glob pattern (repeatable).")
    parser.add_argument("--content-dedupe", action="store_true", help="Skip upload when same content hash exists in S3.")
    return parser.parse_args(argv)


def resolve_inputs(args: argparse.Namespace) -> Tuple[Path, S3Destination]:
    source_input = args.source or input("Local directory to sync: ").strip()
    dest_input = args.dest or input("S3 destination (s3://bucket/prefix): ").strip()
    source = Path(source_input).expanduser().resolve()
    if not source.exists() or not source.is_dir():
        raise ValueError(f"Source directory does not exist or is not a directory: {source}")
    destination = parse_s3_uri(dest_input)
    return source, destination


def create_s3_client(profile: Optional[str], region: Optional[str]) -> Any:
    session_kwargs: Dict[str, Any] = {}
    if profile:
        session_kwargs["profile_name"] = profile
    if region:
        session_kwargs["region_name"] = region
    try:
        session = boto3.Session(**session_kwargs)
    except ProfileNotFound:
        raise ValueError(f"AWS profile not found: {profile}")
    return session.client("s3")


def run_sync(args: argparse.Namespace) -> int:
    source, destination = resolve_inputs(args)
    cache = ShaCache(Path(args.cache).expanduser())
    client = create_s3_client(args.profile, args.region)
    summary = Summary()
    summary_lock = threading.Lock()
    hash_map_lock = threading.Lock()
    run_hash_map: Dict[str, str] = {}

    if args.delete and not args.yes and not args.dry_run and not sys.stdin.isatty():
        print("ERROR --delete requires --yes in non-interactive mode")
        return 2

    files = list(
        iter_local_files(
            source=source,
            follow_symlinks=args.follow_symlinks,
            include=args.include,
            exclude=args.exclude,
            skip_junk=True,
        )
    )
    summary.scanned = len(files)
    local_keys = {destination.key_for(item.rel_posix) for item in files}

    def process_file(file_obj: LocalFile) -> None:
        nonlocal cache
        key = destination.key_for(file_obj.rel_posix)
        s3_uri = destination.uri_for_key(key)
        local_sha_for_decision: Optional[str] = None
        try:
            exists, remote_metadata = head_object_metadata(client, destination.bucket, key)
            if exists and remote_metadata and remote_metadata.get("sha256"):
                local_sha_for_decision = get_local_sha(cache, file_obj)
            needs_upload, reason = choose_upload_action(
                remote_exists=exists,
                remote_metadata=remote_metadata,
                file_obj=file_obj,
                local_sha=local_sha_for_decision,
            )
            if not needs_upload:
                print(f"SKIP  {file_obj.path}  ({reason})")
                if remote_metadata and remote_metadata.get("sha256"):
                    sha = remote_metadata["sha256"]
                    cache.set_sha(file_obj.path, file_obj.size, file_obj.mtime, sha, s3_uri=s3_uri)
                    with hash_map_lock:
                        run_hash_map[sha] = s3_uri
                with summary_lock:
                    summary.skipped += 1
                return

            if args.content_dedupe and not exists:
                sha = get_local_sha(cache, file_obj)
                with hash_map_lock:
                    run_canonical = run_hash_map.get(sha)
                canonical = run_canonical or cache.get_canonical_uri(sha)
                if canonical and canonical.startswith(f"s3://{destination.bucket}/"):
                    canonical_key = canonical.replace(f"s3://{destination.bucket}/", "", 1)
                    if key_exists(client, destination.bucket, canonical_key):
                        print(f"DUPLICATE {file_obj.path} (same as {canonical})")
                        cache.set_sha(file_obj.path, file_obj.size, file_obj.mtime, sha, s3_uri=canonical)
                        with summary_lock:
                            summary.duplicates += 1
                        return
                    cache.set_canonical_uri(sha, None)
                local_sha_for_decision = sha

            sha_to_upload = local_sha_for_decision or get_local_sha(cache, file_obj)
            print(
                f"UPLOAD {file_obj.path} -> {s3_uri} ({format_bytes(file_obj.size)})"
                + (" (dry-run)" if args.dry_run else "")
            )
            success, _ = upload_one(
                client=client,
                file_obj=file_obj,
                bucket=destination.bucket,
                key=key,
                sha256=sha_to_upload,
                dry_run=args.dry_run,
            )
            if success:
                cache.set_sha(file_obj.path, file_obj.size, file_obj.mtime, sha_to_upload, s3_uri=s3_uri)
                with hash_map_lock:
                    run_hash_map[sha_to_upload] = s3_uri
                with summary_lock:
                    summary.uploaded += 1
                    summary.uploaded_bytes += file_obj.size
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {file_obj.path} ({exc})")
            with summary_lock:
                summary.errors += 1

    start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(process_file, item) for item in files]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    if args.delete:
        require_confirmation = not args.yes and not args.dry_run
        deleted, delete_errors = maybe_delete_remote(
            client=client,
            destination=destination,
            local_keys=local_keys,
            dry_run=args.dry_run,
            require_confirmation=require_confirmation,
        )
        summary.deleted += deleted
        summary.errors += delete_errors

    cache.save()
    elapsed = time.time() - start
    print(
        "Summary: "
        f"scanned={summary.scanned} "
        f"uploaded={summary.uploaded} "
        f"skipped={summary.skipped} "
        f"duplicates={summary.duplicates} "
        f"errors={summary.errors} "
        f"bytes_uploaded={format_bytes(summary.uploaded_bytes)} "
        f"deleted={summary.deleted} "
        f"elapsed={elapsed:.1f}s"
    )
    return 1 if summary.errors else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run_sync(args)
    except ValueError as exc:
        print(f"ERROR {exc}")
        return 2
    except KeyboardInterrupt:
        print("ERROR interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
