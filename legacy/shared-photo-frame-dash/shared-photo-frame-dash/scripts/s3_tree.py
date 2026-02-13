#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import boto3


def build_tree(keys: list[str]) -> dict:
    tree = {}
    for key in keys:
        parts = key.strip("/").split("/")
        node = tree
        for part in parts:
            node = node.setdefault(part, {})
    return tree


def print_tree(tree: dict, prefix: str = "", level: int = 0, max_depth: int | None = None):
    if max_depth is not None and level >= max_depth:
        return

    items = sorted(tree.items())
    for idx, (name, subtree) in enumerate(items):
        is_last = idx == len(items) - 1
        connector = "└── " if is_last else "├── "
        print(prefix + connector + name)

        extension = "    " if is_last else "│   "
        print_tree(subtree, prefix + extension, level + 1, max_depth)


def list_all_keys(s3, bucket: str, prefix: str) -> list[str]:
    keys = []
    token = None

    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token

        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])

        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description="Tree-style S3 bucket listing")
    ap.add_argument("--bucket", required=True, help="S3 bucket name")
    ap.add_argument("--prefix", default="", help="Optional prefix (folder)")
    ap.add_argument(
        "-L",
        "--max-depth",
        type=int,
        default=None,
        help="Max depth (like tree -L). Default: unlimited",
    )
    ap.add_argument("--region", default=None, help="AWS region override")

    args = ap.parse_args()

    session = boto3.session.Session(region_name=args.region)
    s3 = session.client("s3")

    keys = list_all_keys(s3, args.bucket, args.prefix)
    if not keys:
        print("(empty)")
        return 0

    tree = build_tree(keys)
    print(args.bucket + (f"/{args.prefix.rstrip('/')}" if args.prefix else ""))
    print_tree(tree, max_depth=args.max_depth)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
