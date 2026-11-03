"""Mint a development bearer token for the operator UI's sign-in screen.

    python -m tools.dev_token --role AR_SPECIALIST
    python -m tools.dev_token --role DENIAL_SPECIALIST --client <uuid> --hours 8

Development only. `rcm.platform.auth.issue_dev_token` refuses outside the
local and ci environments, and the API refuses the token outside them too,
so this cannot open a deployed system. Both this tool and the API read the
same secret from `RCM_DEV_JWT_SECRET` (at least 32 bytes); nothing writes it
to a file (CLAUDE.md section 2).

The default client is the synthetic corpus's (tools/load_corpus.py).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import timedelta

from rcm.platform.auth import Role, issue_dev_token
from rcm.platform.config import get_settings
from tools.load_corpus import DEFAULT_CLIENT

_HUMAN_ROLES = [r.value for r in Role if r is not Role.SYSTEM]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--role", action="append", choices=_HUMAN_ROLES, required=True)
    parser.add_argument("--client", action="append", type=uuid.UUID, default=None)
    parser.add_argument("--subject", default="dev.operator")
    parser.add_argument("--hours", type=float, default=8.0)
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.dev_jwt_secret is None:
        sys.stderr.write(
            "RCM_DEV_JWT_SECRET is not set. Export the same value (32+ bytes) for this "
            "tool and for the API, e.g. in PowerShell:\n"
            '  $env:RCM_DEV_JWT_SECRET = "<a random string of 32 or more characters>"\n'
        )
        return 2
    token = issue_dev_token(
        subject=args.subject,
        roles=[Role(r) for r in args.role],
        client_ids=args.client or [DEFAULT_CLIENT],
        settings=settings,
        expires_in=timedelta(hours=args.hours),
    )
    sys.stdout.write(f"{token}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
