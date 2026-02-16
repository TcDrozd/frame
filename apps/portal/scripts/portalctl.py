#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# to make scripts/portalctl.py behave as if ran from root of repo, set path before importing app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.db import engine, SessionLocal
from app.models.user import User
from app.auth import hash_password

def run(cmd: list[str]) -> int:
    print("+", " ".join(cmd))
    return subprocess.call(cmd)

def cmd_db(args: argparse.Namespace) -> int:
    if args.action == "init":
        # create alembic versions dir if missing
        os.makedirs("alembic/versions", exist_ok=True)
        print("Alembic initialized (versions dir ensured).")
        return 0

    if args.action == "revision":
        return run(["alembic", "-c", "alembic.ini", "revision", "--autogenerate", "-m", args.message])

    if args.action == "upgrade":
        return run(["alembic", "-c", "alembic.ini", "upgrade", "head"])

    if args.action == "downgrade":
        return run(["alembic", "-c", "alembic.ini", "downgrade", args.revision])

    print("Unknown db action")
    return 2

def cmd_seed_admin(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == args.username).first()
        if existing:
            print(f"User {args.username} already exists.")
            return 0
        u = User(username=args.username, password_hash=hash_password(args.password), is_admin=True)
        db.add(u)
        db.commit()
        print(f"Seeded admin user: {args.username}")
        return 0
    finally:
        db.close()

def cmd_reset_password(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == args.username).first()
        if not user:
            print(f"User {args.username} not found.")
            return 1

        user.password_hash = hash_password(args.password)
        db.commit()
        print(f"Password updated for user: {args.username}")
        return 0
    finally:
        db.close()

def main() -> int:
    parser = argparse.ArgumentParser(prog="portalctl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_db = sub.add_parser("db")
    p_db.add_argument("action", choices=["init", "revision", "upgrade", "downgrade"])
    p_db.add_argument("--message", default="init")
    p_db.add_argument("--revision", default="-1")
    p_db.set_defaults(func=cmd_db)

    p_seed = sub.add_parser("seed-admin")
    p_seed.add_argument("--username", required=True)
    p_seed.add_argument("--password", required=True)
    p_seed.set_defaults(func=cmd_seed_admin)

    p_reset = sub.add_parser("reset-password")
    p_reset.add_argument("--username", required=True)
    p_reset.add_argument("--password", required=True)
    p_reset.set_defaults(func=cmd_reset_password)

    args = parser.parse_args()
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())
