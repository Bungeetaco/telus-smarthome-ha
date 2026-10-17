#!/usr/bin/env python3
"""Standalone TELUS SmartHome+ login tester.

Run this on your own machine (NOT in HA) to confirm the headless login works
with your real credentials, and to dump the pieces the integration still needs
to confirm (authdomain value, which x-api-key, device JSON shape).

    pip install requests
    python login_test.py            # it will prompt for email + password

Or set them as env vars to avoid prompts:
    export TELUS_EMAIL=you@example.com
    export TELUS_PASSWORD='...'
    python login_test.py

It prints each step. Nothing is sent anywhere except TELUS. If a step fails it
shows the status + body so we can see exactly which field name to adjust.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import uuid

import requests

HOST = "https://api.end-user.prod.telus.smarthomeplus.com"

# The two app-embedded API keys we found. The script tries them in order.
API_KEYS = [
    "TNUrbgYIhXjDvxDVmTsUcfoojeWfdhBd",  # confirmed gateway key
    "7F90M9NhRG357mdkH61LjVriRX3AkGSe",
]
# authdomain values to try (first that logs in wins).
AUTH_DOMAINS = ["telus", "TELUS", "", "koodo"]

IDP_URL = "/authentication-management/end-user/acr/idpurl"
TOKEN_URL = "/authentication-management/end-user/acr/token"
PROFILES = "/profile-management/profiles/my-profile"
HOUSEHOLDS = "/profile-management/profiles/{pid}/households"
DEVICES = "/presentation-management/household/{hid}/devices"


def cid() -> str:
    return str(uuid.uuid4())


def hdr(api_key: str, **extra) -> dict:
    h = {
        "x-api-key": api_key,
        "Accept": "application/json",
        "x-unified-correlation-id": cid(),
        "User-Agent": "okhttp/4.12.0",
    }
    h.update(extra)
    return h


def pretty(obj) -> str:
    try:
        return json.dumps(obj, indent=2)[:4000]
    except Exception:
        return str(obj)[:4000]


def get_login_url(s: requests.Session, api_key: str) -> str | None:
    r = s.get(HOST + IDP_URL, headers=hdr(api_key), timeout=30)
    print(f"  [idpurl] {r.status_code}")
    if r.status_code != 200:
        print("   body:", r.text[:300])
        return None
    data = r.json()
    url = data.get("loginUrl") or (data.get("body") or {}).get("idpUrl")
    print("   loginUrl:", url)
    print("   idpParameters:", pretty(data.get("idpParameters") or data.get("body")))
    return url


def login(s, login_url, email, password, authdomain) -> str | None:
    body = {"authdomain": authdomain, "loginid": email, "password": password}
    h = {
        "Accept": "application/json",
        "Accept-Language": "en-CA",
        "Content-Type": "application/json",
        "x-unified-correlation-id": cid(),
        "User-Agent": "okhttp/4.12.0",
    }
    r = s.post(login_url, json=body, headers=h, timeout=30, allow_redirects=False)
    print(f"  [login authdomain={authdomain!r}] {r.status_code}")
    set_cookies = r.headers.get("Set-Cookie")
    jar = "; ".join(f"{c.name}={c.value}" for c in s.cookies)
    if r.status_code in (200, 201, 204) and (set_cookies or jar):
        print("   got cookies:", (set_cookies or jar)[:120], "...")
        return jar or set_cookies
    print("   body:", r.text[:300])
    return None


def token_from_cookies(s, api_key, cookie_header) -> dict | None:
    r = s.get(
        HOST + TOKEN_URL,
        headers=hdr(api_key, Cookie=cookie_header),
        timeout=30,
    )
    print(f"  [token] {r.status_code}")
    if r.status_code != 200:
        print("   body:", r.text[:300])
        return None
    data = r.json()
    tok = data.get("data") or data.get("body") or data
    print("   accessToken:", (tok.get("accessToken") or "")[:40], "...")
    print("   refreshToken present:", bool(tok.get("refreshToken")))
    return tok


def main() -> int:
    email = os.environ.get("TELUS_EMAIL") or input("TELUS email: ").strip()
    password = os.environ.get("TELUS_PASSWORD") or getpass.getpass("TELUS password: ")

    for api_key in API_KEYS:
        print(f"\n=== Trying x-api-key {api_key[:6]}... ===")
        s = requests.Session()
        login_url = get_login_url(s, api_key)
        if not login_url:
            continue
        for ad in AUTH_DOMAINS:
            cookies = login(s, login_url, email, password, ad)
            if not cookies:
                continue
            tok = token_from_cookies(s, api_key, cookies)
            if not tok or not tok.get("accessToken"):
                continue

            # Success — now dump profile/household/devices so we see the shapes.
            access = tok["accessToken"]
            auth = hdr(api_key, Authorization=f"Bearer {access}")
            prof = s.get(HOST + PROFILES, headers=auth, timeout=30)
            print("\n  [my-profile]", prof.status_code)
            print(pretty(prof.json() if prof.ok else prof.text))
            pid = (prof.json() or {}).get("profileId") if prof.ok else None
            if pid:
                hh = s.get(HOST + HOUSEHOLDS.format(pid=pid), headers=auth, timeout=30)
                print("\n  [households]", hh.status_code)
                print(pretty(hh.json() if hh.ok else hh.text))
                hid = None
                try:
                    lst = hh.json()
                    lst = lst if isinstance(lst, list) else lst.get("data") or []
                    hid = (lst[0].get("householdId") or lst[0].get("id")) if lst else None
                except Exception:
                    pass
                if hid:
                    auth2 = hdr(
                        api_key,
                        Authorization=f"Bearer {access}",
                        **{"x-unified-household-id": hid},
                    )
                    dev = s.get(HOST + DEVICES.format(hid=hid), headers=auth2, timeout=30)
                    print("\n  [devices]", dev.status_code)
                    print(pretty(dev.json() if dev.ok else dev.text))

            print("\n\n✅ SUCCESS")
            print(f"   working x-api-key : {api_key}")
            print(f"   working authdomain: {ad!r}")
            print("   -> put these in const.py (PLATFORM_API_KEY, DEFAULT_AUTH_DOMAIN)")
            print("   -> paste the [devices] JSON back to Claude to finalize entities")
            return 0

    print("\n❌ No combination logged in. Paste the output above to Claude.")
    print("   (If [idpurl] itself failed, the host or x-api-key is the issue;")
    print("    if [login] failed, it's the authdomain/credential shape.)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
