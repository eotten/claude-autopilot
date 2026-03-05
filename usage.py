"""Check Claude Code usage via the OAuth API."""

import json
import subprocess
import platform
import urllib.request
import urllib.error
import ssl
import time

_token_cache = None
_usage_cache = None
_usage_cache_time = 0
_CACHE_TTL = 30  # seconds


def get_oauth_token():
    """Retrieve the Claude Code OAuth token from the system keychain."""
    global _token_cache
    if _token_cache:
        return _token_cache

    system = platform.system()

    if system == "Darwin":
        for extra_args in [[], ["-a", "eric"]]:
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "Claude Code-credentials", *extra_args, "-w"],
                    capture_output=True, text=True
                )
                if result.returncode == 0 and result.stdout.strip():
                    creds = json.loads(result.stdout.strip())
                    oauth = creds.get("claudeAiOauth", creds)
                    token = oauth.get("accessToken") or oauth.get("access_token")
                    if token:
                        _token_cache = token
                        return token
            except (json.JSONDecodeError, FileNotFoundError):
                continue

    elif system == "Linux":
        try:
            result = subprocess.run(
                ["secret-tool", "lookup", "service", "Claude Code-credentials"],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                creds = json.loads(result.stdout.strip())
                oauth = creds.get("claudeAiOauth", creds)
                token = oauth.get("accessToken") or oauth.get("access_token")
                if token:
                    _token_cache = token
                    return token
        except (json.JSONDecodeError, FileNotFoundError):
            pass

    return None


def get_usage():
    """Query the Claude usage API. Returns dict or None on failure. Cached for 30s."""
    global _usage_cache, _usage_cache_time

    now = time.time()
    if _usage_cache and (now - _usage_cache_time) < _CACHE_TTL:
        return _usage_cache

    token = get_oauth_token()
    if not token:
        return None

    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass

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

        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            _usage_cache = data
            _usage_cache_time = now
            return data

    except Exception:
        return None


def get_five_hour_utilization():
    """Return the 5-hour session utilization as a percentage (0-100), or None if unavailable."""
    data = get_usage()
    if data and "five_hour" in data:
        return data["five_hour"].get("utilization")
    return None


def is_under_usage_limit(max_utilization):
    """Check if 5-hour utilization is under the given threshold. Returns True if unknown (fail-open)."""
    util = get_five_hour_utilization()
    if util is None:
        return True  # Can't check — don't block
    return util < max_utilization
