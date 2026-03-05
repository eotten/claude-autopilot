#!/usr/bin/env python3
"""Test script to check Claude Code usage via the OAuth API."""

import json
import subprocess
import sys
import platform

def get_oauth_token():
    """Retrieve the Claude Code OAuth token from the system keychain."""
    system = platform.system()

    if system == "Darwin":
        # macOS Keychain — try with and without account name
        for extra_args in [["-a", "eric"], []]:
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "Claude Code-credentials", *extra_args, "-w"],
                    capture_output=True, text=True
                )
                if result.returncode == 0 and result.stdout.strip():
                    creds = json.loads(result.stdout.strip())
                    # Token may be nested under claudeAiOauth
                    oauth = creds.get("claudeAiOauth", creds)
                    return oauth.get("accessToken") or oauth.get("access_token")
            except (json.JSONDecodeError, FileNotFoundError):
                continue

    elif system == "Linux":
        # Linux secret-tool
        try:
            result = subprocess.run(
                ["secret-tool", "lookup", "service", "Claude Code-credentials"],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                creds = json.loads(result.stdout.strip())
                oauth = creds.get("claudeAiOauth", creds)
                return oauth.get("accessToken") or oauth.get("access_token")
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    return None


def check_usage(token):
    """Query the Claude usage API and return the response."""
    import urllib.request
    import urllib.error
    import ssl
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())

    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "claude-autopilot/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            data = json.loads(resp.read().decode())
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"HTTP {e.code}: {e.reason}")
        print(f"Body: {body}")
        return None


if __name__ == "__main__":
    print("Looking for OAuth token...")
    token = get_oauth_token()

    if not token:
        print("Could not find OAuth token in keychain.")
        print("Make sure you've authenticated Claude Code at least once (`claude` then log in).")
        sys.exit(1)

    print(f"Found token: {token[:20]}...")
    print()
    print("Querying usage API...")
    data = check_usage(token)

    if data:
        print()
        print("Raw response:")
        print(json.dumps(data, indent=2))
    else:
        print("Failed to get usage data.")
        sys.exit(1)
