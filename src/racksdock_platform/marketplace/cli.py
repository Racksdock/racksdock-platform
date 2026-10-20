from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import Settings
from .models import User
from .services.auth import hash_password, new_api_key


def main() -> int:
    p = argparse.ArgumentParser(prog="racksdock-marketplace")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    u = sub.add_parser("create-user")
    u.add_argument("email")
    u.add_argument("--password", default="")
    u.add_argument("--role", default="user")
    u.add_argument("--credits-cents", type=int, default=0)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if args.cmd == "serve":
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
        return 0
    if args.cmd == "create-user":
        app = create_app(settings, background=False)
        with app.state.db.session() as sess:
            user = User(email=args.email, password_hash=hash_password(args.password or "x"), role=args.role, credits_cents=args.credits_cents)
            sess.add(user)
            sess.flush()
            key = new_api_key(sess, user)
            print(f"user_id={user.id} api_key={key}")
        return 0
    return 1
