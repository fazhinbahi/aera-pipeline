"""
Aera headless authentication.

Logs into Aera using credentials from .env, gets a fresh JSESSIONID +
access_token, and saves them to .aera_token.json.

Usage:
  python3 aera_auth.py          # login and save token
  from aera_auth import ensure_token  # call from fetch scripts
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

BASE_URL   = "https://becleproximo.aeratechnology.com"
AUTH_URL   = f"{BASE_URL}/ispring/awc?ServiceName=Authenticate"
TOKEN_URL  = f"{BASE_URL}/ispring/client/oauth/token"
APP_ID     = "695EB357-4AE4-11ED-BCC9-0A3087F18497"
PRJ_ID     = "43A40AB0_D908_45C0_9B06_32ABBB10B0FD"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aera_token.json")
ENV_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_credentials() -> tuple[str, str]:
    load_dotenv(ENV_FILE)
    username = os.getenv("AERA_USERNAME", "")
    password = os.getenv("AERA_PASSWORD", "")
    if not username or not password:
        print(f"✗ Credentials not found in {ENV_FILE}")
        sys.exit(1)
    return username, password


def login() -> dict:
    """Log in to Aera, return dict with access_token, jsessionid, expires_at."""
    username, password = _load_credentials()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Origin":     BASE_URL,
        "Referer":    BASE_URL,
    })

    # Step 1 — authenticate (sets JSESSIONID cookie)
    print(f"  Logging in as {username}...")
    resp = session.post(
        AUTH_URL,
        data={"loginname": username, "pwd": password, "prj": PRJ_ID},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        msg = body.get("message", "Unknown error")
        print(f"✗ Login failed: {msg}")
        sys.exit(1)

    jsessionid = session.cookies.get("JSESSIONID", "")
    lb_instance_id = session.cookies.get("lb-instance-id", "")
    if not jsessionid:
        print("✗ No JSESSIONID in login response — check credentials")
        sys.exit(1)

    # Step 2 — exchange session for OAuth access_token
    resp2 = session.post(
        TOKEN_URL,
        data={"grant_type": "custom_session_id", "app_id": APP_ID},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp2.raise_for_status()
    tok = resp2.json()
    access_token   = tok.get("access_token", "")
    refresh_token  = tok.get("refresh_token", "")
    expires_in_ms  = int(tok.get("expires_in", 2700000))

    token_data = {
        "access_token":   access_token,
        "refresh_token":  refresh_token,
        "jsessionid":     jsessionid,
        "lb_instance_id": lb_instance_id,
        "expires_at":     time.time() + expires_in_ms / 1000,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    remaining = int(expires_in_ms / 1000)
    print(f"✓ Logged in — token valid for {remaining}s / {remaining // 60} min")
    return token_data


def ensure_token(min_seconds: int = 300) -> dict:
    """Return a valid token, auto-logging in if expired or nearly expired."""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                cached = json.load(f)
            remaining = cached.get("expires_at", 0) - time.time()
            if remaining > min_seconds:
                print(f"✓ Token valid (expires in {int(remaining)}s)")
                return cached
            print(f"  Token expires in {int(remaining)}s — refreshing via login...")
        except Exception:
            pass
    else:
        print("  No token file found — logging in...")

    return login()


if __name__ == "__main__":
    login()
