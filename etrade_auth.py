#!/usr/bin/env python3
"""Secure, interactive E*TRADE OAuth authorization."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pyetrade
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"


def update_env_tokens(path: Path, access_token: str, access_secret: str) -> None:
    """Atomically update only the two daily access-token entries."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacements = {
        "ETRADE_ACCESS_TOKEN": access_token,
        "ETRADE_ACCESS_TOKEN_SECRET": access_secret,
    }
    seen = set()
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in replacements:
            output.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in replacements.items():
        if key not in seen:
            output.append(f"{key}={value}")
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temp.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Authorize E*TRADE for the current day.")
    parser.add_argument(
        "--no-write-env",
        action="store_true",
        help="Do not update .env after authorization.",
    )
    parser.add_argument(
        "--print-tokens",
        action="store_true",
        help="Print token secrets to the terminal (normally avoided).",
    )
    args = parser.parse_args(argv)

    load_dotenv(ENV_PATH)
    consumer_key = os.getenv("ETRADE_CONSUMER_KEY")
    consumer_secret = os.getenv("ETRADE_CONSUMER_SECRET")
    dev = os.getenv("ETRADE_DEV", "false").lower() == "true"
    if not consumer_key or not consumer_secret:
        raise ValueError(
            "Missing ETRADE_CONSUMER_KEY or ETRADE_CONSUMER_SECRET in .env"
        )

    print(f"Using {'Sandbox' if dev else 'Production'} environment\n")
    oauth = pyetrade.ETradeOAuth(consumer_key, consumer_secret)
    auth_url = oauth.get_request_token()
    print("Open this URL, log in, and authorize the application:\n")
    print(auth_url)
    verifier = input("\nPaste the verification code: ").strip()
    if not verifier:
        raise ValueError("No verification code provided.")

    tokens = oauth.get_access_token(verifier)
    access_token = tokens["oauth_token"]
    access_secret = tokens["oauth_token_secret"]

    if not args.no_write_env:
        update_env_tokens(ENV_PATH, access_token, access_secret)
        print(f"\nAuthorization succeeded; daily tokens were saved to {ENV_PATH.name}.")
    else:
        print("\nAuthorization succeeded; .env was not changed.")
    if args.print_tokens:
        print(f"ETRADE_ACCESS_TOKEN={access_token}")
        print(f"ETRADE_ACCESS_TOKEN_SECRET={access_secret}")
    elif args.no_write_env:
        print("Re-run with --print-tokens to display them, or omit --no-write-env to save them.")
    print("Tokens expire at midnight US Eastern.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
