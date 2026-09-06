#!/usr/bin/env python3
"""
One-time OAuth2 authorization flow for Polar AccessLink.

Opens a browser for the user to log in to Polar Flow and approve access,
catches the redirect locally, exchanges the auth code for an access token,
registers the user with AccessLink (required once), and writes
POLAR_ACCESS_TOKEN / POLAR_USER_ID into .env.

Usage:
    python polar_oauth_setup.py
"""

import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

ENV_PATH = Path(__file__).parent / ".env"
REDIRECT_URI = "http://localhost:8080/callback"
AUTH_URL = "https://flow.polar.com/oauth2/authorization"
TOKEN_URL = "https://polarremote.com/v2/oauth2/token"
REGISTER_URL = "https://www.polaraccesslink.com/v3/users"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def update_env(updates: dict):
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    seen = set()
    new_lines = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(new_lines) + "\n")


class CallbackHandler(BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        CallbackHandler.code = qs.get("code", [None])[0]
        CallbackHandler.error = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if CallbackHandler.code:
            body = "<html><body>Polar authorization complete. You can close this tab.</body></html>"
        else:
            body = f"<html><body>Authorization failed: {CallbackHandler.error}</body></html>"
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # keep terminal quiet


def main():
    env = load_env()
    client_id = env.get("POLAR_CLIENT_ID")
    client_secret = env.get("POLAR_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("POLAR_CLIENT_ID / POLAR_CLIENT_SECRET missing from .env")

    auth_url = (
        f"{AUTH_URL}?response_type=code&client_id={client_id}"
        f"&redirect_uri={REDIRECT_URI}&scope=accesslink.read_all"
    )
    print("Opening browser for Polar authorization...")
    print(f"If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    print("Waiting for authorization redirect on http://localhost:8080/callback ...")
    server.handle_request()  # blocks until one request arrives

    if not CallbackHandler.code:
        sys.exit(f"Authorization failed: {CallbackHandler.error}")

    print("Got authorization code, exchanging for access token...")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": CallbackHandler.code,
            "redirect_uri": REDIRECT_URI,
        },
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    token_data = resp.json()
    access_token = token_data["access_token"]
    user_id = token_data.get("x_user_id")
    print(f"Got access token (user_id={user_id}).")

    print("Registering user with AccessLink (one-time; ok if already registered)...")
    reg = requests.post(
        REGISTER_URL,
        json={"member-id": f"hiit-pipeline-{user_id}"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if reg.status_code in (200, 201):
        print("User registered.")
    elif reg.status_code == 409:
        print("User was already registered.")
    else:
        print(f"Warning: registration returned {reg.status_code}: {reg.text}")

    update_env({"POLAR_ACCESS_TOKEN": access_token, "POLAR_USER_ID": str(user_id)})
    print(f"Saved POLAR_ACCESS_TOKEN (…{access_token[-6:]}) and POLAR_USER_ID={user_id} to .env")


if __name__ == "__main__":
    main()
