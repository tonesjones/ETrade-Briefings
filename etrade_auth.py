#!/usr/bin/env python3
"""
E*TRADE OAuth authorization script.

Run this to get access tokens, then add them to .env.

IMPORTANT: E*TRADE access tokens expire at midnight US Eastern every day.
You must re-run this (browser login + paste verification code + update .env)
once per calendar day after expiry. Same-day portfolio fetches reuse .env tokens.

READ-ONLY NOTE
--------------
This script only obtains OAuth tokens (request token → access token).
It does not place, preview, or cancel any orders.
"""

import os
from dotenv import load_dotenv
import pyetrade

load_dotenv()

consumer_key = os.getenv("ETRADE_CONSUMER_KEY")
consumer_secret = os.getenv("ETRADE_CONSUMER_SECRET")
dev = os.getenv("ETRADE_DEV", "false").lower() == "true"

if not consumer_key or not consumer_secret:
    raise ValueError(
        "Missing ETRADE_CONSUMER_KEY or ETRADE_CONSUMER_SECRET in .env\n"
        "Copy .env.example to .env and fill in your keys first."
    )

print(f"Using {'Sandbox' if dev else 'Production'} environment\n")

oauth = pyetrade.ETradeOAuth(consumer_key, consumer_secret)

print("1. Opening authorization URL...")
auth_url = oauth.get_request_token()
print("\nOpen this URL in your browser, log in, and authorize the application:\n")
print(auth_url)
print()

verifier = input("2. Paste the verification code from the browser here: ").strip()

if not verifier:
    raise ValueError("No verification code provided.")

tokens = oauth.get_access_token(verifier)

print("\n" + "=" * 60)
print("SUCCESS! Replace these two lines in your .env file:")
print("=" * 60)
print(f"ETRADE_ACCESS_TOKEN={tokens['oauth_token']}")
print(f"ETRADE_ACCESS_TOKEN_SECRET={tokens['oauth_token_secret']}")
print("=" * 60)
print("\nTokens expire at midnight US Eastern — re-run this script after that.")
print("Same day: no re-auth needed. Next: python build_briefing_prompt.py")
