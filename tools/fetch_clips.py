#!/usr/bin/env python3
"""Download TELUS SmartHome+ recorded clips (cloud recordings) to MP4.

Runs on YOUR machine with YOUR credentials — nothing is sent anywhere except
TELUS. It reuses the same headless login as the integration, lists recent
recordings for your camera(s), and can download any of them to an .mp4 using
the clip's ready-made HLS URL (no WebRTC / no bridge needed).

    pip install requests          # ffmpeg must be on PATH for --get
    export TELUS_EMAIL=you@example.com
    export TELUS_PASSWORD='...'

    python fetch_clips.py                    # list the 25 most recent clips
    python fetch_clips.py --max 50           # list more
    python fetch_clips.py --device odc-XXXX  # only one camera
    python fetch_clips.py --get latest       # download the newest clip
    python fetch_clips.py --get all          # download everything listed
    python fetch_clips.py --get 3            # download row #3 from the list
    python fetch_clips.py --get <clipId>     # download a specific clip id
    python fetch_clips.py --out ~/telus_clips

Add --debug to dump the raw JSON (useful if TELUS changes a field name).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import requests

HOST = "https://api.end-user.prod.telus.smarthomeplus.com"
API_KEY = "TNUrbgYIhXjDvxDVmTsUcfoojeWfdhBd"   # confirmed Platform key
AUTH_DOMAIN = "telus"

IDP_URL = "/authentication-management/end-user/acr/idpurl"
TOKEN_URL = "/authentication-management/end-user/acr/token"
MY_PROFILE = "/profile-management/profiles/my-profile"
HOUSEHOLDS = "/profile-management/profiles/{pid}/households"
CLIPS = "/video-management/clips/{hid}"
CLIP_HLS = "/video-management/streaming/{hid}/clip/{clip_id}"

DEBUG = False


def cid() -> str:
    return str(uuid.uuid4())


def hdr(**extra) -> dict:
    h = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
        "x-unified-correlation-id": cid(),
        "User-Agent": "okhttp/4.12.0",
    }
    h.update(extra)
    return h


def dbg(label, obj):
    if DEBUG:
        try:
            print(f"--- {label} ---\n{json.dumps(obj, indent=2)[:4000]}\n", file=sys.stderr)
        except Exception:
            print(f"--- {label} ---\n{str(obj)[:2000]}\n", file=sys.stderr)


# --------------------------------------------------------------------------- #
# auth (headless email/password -> bearer token)
# --------------------------------------------------------------------------- #
def login(s: requests.Session, email: str, password: str) -> str:
    r = s.get(HOST + IDP_URL, headers=hdr(), timeout=30)
    r.raise_for_status()
    login_url = r.json().get("loginUrl") or (r.json().get("body") or {}).get("idpUrl")
    if not login_url:
        raise SystemExit("could not get login URL (idpurl shape changed)")

    h = {
        "Accept": "application/json",
        "Accept-Language": "en-CA",
        "Content-Type": "application/json",
        "x-unified-correlation-id": cid(),
        "User-Agent": "okhttp/4.12.0",
    }
    body = {"authdomain": AUTH_DOMAIN, "loginid": email, "password": password}
    r = s.post(login_url, json=body, headers=h, timeout=30, allow_redirects=False)
    if r.status_code not in (200, 201, 204):
        raise SystemExit(f"login failed {r.status_code}: {r.text[:300]}")
    cookie_header = "; ".join(f"{c.name}={c.value}" for c in s.cookies) or r.headers.get(
        "Set-Cookie", ""
    )

    r = s.get(HOST + TOKEN_URL, headers=hdr(Cookie=cookie_header), timeout=30)
    r.raise_for_status()
    tok = r.json().get("data") or r.json().get("body") or r.json()
    access = tok.get("accessToken")
    if not access:
        raise SystemExit("no access token returned")
    return access


def _first_household_id(data):
    """Same lookup the HA integration uses (profile, households[], bare list)."""
    if not isinstance(data, (dict, list)):
        return None
    cands = []
    if isinstance(data, list):
        cands = data
    else:
        for key in ("households", "data", "body", "items", "results"):
            val = data.get(key)
            if isinstance(val, list):
                cands = val
                break
            if isinstance(val, dict) and isinstance(val.get("households"), list):
                cands = val["households"]
                break
    for item in cands:
        if isinstance(item, dict):
            hid = item.get("householdId") or item.get("id")
            if hid:
                return hid
        elif isinstance(item, str):
            return item
    return None


def resolve_household(s: requests.Session, access: str) -> str:
    auth = hdr(Authorization=f"Bearer {access}")
    r = s.get(HOST + MY_PROFILE, headers=auth, timeout=30)
    r.raise_for_status()
    data = r.json()
    dbg("my-profile", data)
    root = data
    if isinstance(root, dict) and "profile" not in root:
        root = root.get("data") or root.get("body") or root
    profile = (root.get("profile") if isinstance(root, dict) else None) or {}
    pid = profile.get("profileId") or profile.get("id") or profile.get("uuid")
    hid = profile.get("householdId") or _first_household_id(root)
    if hid:
        return hid
    if pid:
        r = s.get(HOST + HOUSEHOLDS.format(pid=pid), headers=auth, timeout=30)
        r.raise_for_status()
        hid = _first_household_id(r.json())
        if hid:
            return hid
    keys = list(root) if isinstance(root, dict) else type(root).__name__
    raise SystemExit(
        f"could not resolve household id (profile keys: {keys}). "
        "Re-run with --debug, or pass it directly with --household <id>."
    )


# --------------------------------------------------------------------------- #
# clips
# --------------------------------------------------------------------------- #
def _as_items(data) -> list[dict]:
    """Unwrap the clip-list payload to a list of clip dicts (shape-tolerant)."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("clips", "items", "results", "data", "body", "events", "recordings"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                nested = _as_items(v)
                if nested:
                    return nested
    return []


def _clip_id(c: dict):
    for k in ("clipId", "id", "clip_id", "eventId", "recordingId"):
        if c.get(k):
            return str(c[k])
    return None


def _clip_time(c: dict):
    for k in ("startTime", "startTimestamp", "timestamp", "createdAt", "eventTime", "time"):
        v = c.get(k)
        if v:
            # epoch ms, epoch s, or ISO string
            if isinstance(v, (int, float)):
                ts = v / 1000 if v > 1e12 else v
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except Exception:
                return str(v)
    return None


def _clip_event(c: dict):
    kw = c.get("keywords")
    if isinstance(kw, list) and kw:
        return "+".join(str(k).title() for k in kw)
    for k in ("eventName", "eventType", "type", "label", "detectionType", "category"):
        if c.get(k):
            return str(c[k])
    return ""


def list_clips(s, access, hid, device_id, max_results) -> list[dict]:
    auth = hdr(Authorization=f"Bearer {access}", **{"x-unified-household-id": hid})
    params = {"thumbnail": "MOBILE", "maxResults": str(max_results)}
    if device_id:
        params["deviceId"] = device_id
    r = s.get(HOST + CLIPS.format(hid=hid), headers=auth, params=params, timeout=30)
    if not r.ok:
        raise SystemExit(f"clip list failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    dbg("clips", data)
    return _as_items(data)


def clip_hls_url(s, access, hid, clip_id) -> str | None:
    auth = hdr(Authorization=f"Bearer {access}", **{"x-unified-household-id": hid})
    r = s.get(
        HOST + CLIP_HLS.format(hid=hid, clip_id=clip_id),
        headers=auth,
        params={"protocol": "HLS"},
        timeout=30,
    )
    if not r.ok:
        print(f"  ! streaming url failed {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    dbg("clip-hls", data)
    # find an .m3u8 (or any http url) anywhere in the response
    blob = json.dumps(data)
    m = re.search(r'https?://[^\s"\']+\.m3u8[^\s"\']*', blob)
    if m:
        return m.group(0)
    m = re.search(r'https?://[^\s"\']+', blob)
    return m.group(0) if m else None


def download(url: str, out_path: str) -> bool:
    print(f"  downloading -> {out_path}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
           "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", out_path]
    try:
        subprocess.run(cmd, check=True)
        return True
    except FileNotFoundError:
        print("  ! ffmpeg not found on PATH — install it to download.")
    except subprocess.CalledProcessError as e:
        print(f"  ! ffmpeg failed ({e.returncode}). Try: ffmpeg -i '{url}' -c copy out.mp4")
    return False


# --------------------------------------------------------------------------- #
def main() -> int:
    global DEBUG
    ap = argparse.ArgumentParser(description="Fetch TELUS SmartHome+ recorded clips.")
    ap.add_argument("--device", help="limit to one camera deviceId (e.g. odc-XXXX)")
    ap.add_argument("--household", help="household id (skips auto-detect)")
    ap.add_argument("--max", type=int, default=25, help="how many recent clips to list")
    ap.add_argument("--get", help="'latest' | 'all' | row number | a clipId")
    ap.add_argument("--out", default="telus_clips", help="output folder for downloads")
    ap.add_argument("--debug", action="store_true", help="dump raw JSON to stderr")
    args = ap.parse_args()
    DEBUG = args.debug

    email = os.environ.get("TELUS_EMAIL") or input("TELUS email: ").strip()
    password = os.environ.get("TELUS_PASSWORD") or getpass.getpass("TELUS password: ")

    s = requests.Session()
    print("Logging in...")
    access = login(s, email, password)
    hid = args.household or resolve_household(s, access)
    print(f"Household: {hid}")

    clips = list_clips(s, access, hid, args.device, args.max)
    if not clips:
        print("No clips returned. Re-run with --debug to see the raw response "
              "(TELUS may have renamed a field).")
        return 1

    print(f"\n{len(clips)} recent clip(s):\n")
    print(f"{'#':>3}  {'time (UTC)':<20} {'event':<18} clipId")
    print("-" * 70)
    for i, c in enumerate(clips):
        t = _clip_time(c)
        tstr = t.strftime("%Y-%m-%d %H:%M:%S") if isinstance(t, datetime) else str(t or "")
        print(f"{i:>3}  {tstr:<20} {_clip_event(c):<18} {_clip_id(c)}")

    if not args.get:
        print("\nTo download: --get latest | --get all | --get <row#> | --get <clipId>")
        return 0

    # pick which clips to fetch
    targets: list[dict]
    if args.get == "latest":
        targets = [clips[0]]
    elif args.get == "all":
        targets = clips
    elif args.get.isdigit() and int(args.get) < len(clips):
        targets = [clips[int(args.get)]]
    else:
        targets = [c for c in clips if _clip_id(c) == args.get] or []
    if not targets:
        print(f"Nothing matched --get {args.get!r}.")
        return 1

    os.makedirs(args.out, exist_ok=True)
    ok = 0
    for c in targets:
        clip_id = _clip_id(c)
        t = _clip_time(c)
        stamp = t.strftime("%Y%m%d_%H%M%S") if isinstance(t, datetime) else "clip"
        ev = (_clip_event(c) or "clip").lower()
        name = f"{stamp}_{ev}_{clip_id}.mp4".replace("/", "_")
        print(f"\n[{clip_id}] {ev} @ {stamp}")
        url = clip_hls_url(s, access, hid, clip_id)
        if url and download(url, os.path.join(args.out, name)):
            ok += 1
    print(f"\nDone. {ok}/{len(targets)} downloaded into {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
