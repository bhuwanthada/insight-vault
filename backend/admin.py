"""Operator command-line tool.

    python -m backend.admin keygen --out keys/                       create the signing key pair (keep the private half offline)
    python -m backend.admin mint --private-key keys/org-key-private.pem --org acme --name "Acme Inc" ...
    python -m backend.admin inspect <key> [--public-key keys/org-key-public.pem]
    python -m backend.admin secrets-key                              a master key for SECRETS_KEY
    python -m backend.admin migrate                                  create or upgrade the database schema
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import orgkeys, schema, secrets_box
from .config import FEATURES, ConfigError, Settings
from .db import make_engine


def _quota(text):
    name, _, value = text.partition("=")
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"quota must look like name=number (for example tickets=500), got '{text}'")
    return name, int(value)


def _parser():
    parser = argparse.ArgumentParser(prog="python -m backend.admin", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="create the signing key pair")
    keygen.add_argument("--out", default=".", help="folder to write org-key-private.pem and org-key-public.pem")

    mint = sub.add_parser("mint", help="create a signed organization or license key")
    mint.add_argument("--private-key", required=True, help="path to org-key-private.pem")
    mint.add_argument("--org", required=True, help="organization id (lowercase letters, digits, - or _)")
    mint.add_argument("--name", default="", help="organization display name")
    mint.add_argument("--plan", default="starter")
    mint.add_argument("--role", default="editor", help="role granted when the key is used as an API credential")
    mint.add_argument("--mode", default="tenant", choices=orgkeys.MODES, help="tenant = SaaS organization, license = single-company installation")
    mint.add_argument("--features", default=",".join(FEATURES), help="comma-separated features to enable")
    mint.add_argument("--quota", action="append", type=_quota, default=[], help="name=number, repeatable (tickets, llm_tokens_per_day, seats, connectors)")
    mint.add_argument("--domain", action="append", default=[], help="an e-mail domain of the organization, repeatable")
    mint.add_argument("--admin", action="append", default=[], help="e-mail of an administrator, repeatable")
    mint.add_argument("--days", type=int, default=365, help="validity in days")

    inspect = sub.add_parser("inspect", help="show the sections of a key")
    inspect.add_argument("key")
    inspect.add_argument("--public-key", help="verify the signature with this public key")

    sub.add_parser("secrets-key", help="print a new master key for SECRETS_KEY")
    sub.add_parser("migrate", help="create or upgrade the database schema (uses DATABASE_URL)")
    return parser


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv=None, out=None):
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    try:
        if args.command == "keygen":
            private_pem, public_pem = orgkeys.generate_keypair()
            os.makedirs(args.out, exist_ok=True)
            private_path, public_path = (os.path.join(args.out, n) for n in ("org-key-private.pem", "org-key-public.pem"))
            with open(private_path, "w", encoding="utf-8") as handle:
                handle.write(private_pem)
            os.chmod(private_path, 0o600)
            with open(public_path, "w", encoding="utf-8") as handle:
                handle.write(public_pem)
            print(f"Wrote {private_path} (KEEP OFFLINE: it signs keys) and {public_path} (ship with the app).", file=out)
            print(f"Set ORG_KEY_PUBLIC_KEY_FILE={public_path} on the server.", file=out)
        elif args.command == "mint":
            token = orgkeys.mint(
                _read(args.private_key), org_id=args.org, org_name=args.name, plan=args.plan, role=args.role, mode=args.mode,
                features=[f.strip() for f in args.features.split(",") if f.strip()], quotas=dict(args.quota),
                domains=args.domain, admins=args.admin, days=args.days)
            print(token, file=out)
        elif args.command == "inspect":
            if args.public_key:
                print(json.dumps(orgkeys.verify(args.key, _read(args.public_key)), indent=2), file=out)
                print("signature and expiry: valid", file=out)
            else:
                print(json.dumps(orgkeys.inspect(args.key), indent=2), file=out)
                print("WARNING: not verified. Pass --public-key to check the signature.", file=out)
        elif args.command == "secrets-key":
            print(secrets_box.generate_key(), file=out)
        else:  # migrate
            settings = Settings.from_env()
            if not settings.database_url:
                raise ConfigError("DATABASE_URL is not set")
            applied = schema.migrate(make_engine(settings.database_url, settings.db_pool_size), datetime.now(timezone.utc).isoformat())
            print(f"applied migrations: {applied or 'none (already up to date)'}", file=out)
    except (orgkeys.OrgKeyError, ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
