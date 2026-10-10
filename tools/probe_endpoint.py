"""Probe a mandi-price endpoint and print what it REALLY returns (no assumptions).

Examples (run on your own machine / from a runner you want to test):
    python tools/probe_endpoint.py https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070 \
        api-key=%DATA_GOV_API_KEY% format=json limit=2 "filters[state.keyword]=Gujarat"

    # A URL copied from your browser's DevTools -> Network tab on the Agmarknet 2.0 site:
    python tools/probe_endpoint.py "https://api.agmarknet.gov.in/v1/....?....."

Prints: DNS result, TCP connect result, HTTP status, content-type, size, JSON top-level keys,
first record. The api-key value is masked in the output.
"""
import json
import socket
import sys
import time
from urllib.parse import urlparse

import requests


def mask(text, params):
    for k, v in params.items():
        if "key" in k.lower() and v:
            text = text.replace(v, "***")
    return text


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    url, params = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:] if "=" in a)
    host = urlparse(url).hostname
    try:
        ip = socket.gethostbyname(host)
        print(f"DNS      : {host} -> {ip}")
        with socket.create_connection((ip, 443), timeout=10):
            print("TCP 443  : connected")
    except OSError as exc:
        print(f"DNS/TCP  : FAILED ({exc})")
    t0 = time.time()
    try:
        r = requests.get(url, params=params, timeout=(10, 40),
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    except requests.RequestException as exc:
        sys.exit(f"HTTP     : FAILED {mask(type(exc).__name__ + ': ' + str(exc)[:300], params)}")
    print(f"HTTP     : {r.status_code} in {time.time() - t0:.1f}s, {len(r.content)} bytes")
    print(f"Type     : {r.headers.get('Content-Type')}")
    try:
        data = r.json()
    except ValueError:
        print("Body     : NOT JSON. First 300 chars:\n", r.text[:300])
        return
    if isinstance(data, dict):
        print("JSON keys:", list(data.keys()))
        for k, v in data.items():
            if isinstance(v, list) and v:
                print(f"First item of '{k}' ({len(v)} items):\n", json.dumps(v[0], indent=2)[:800])
                break
        else:
            print(json.dumps(data, indent=2)[:800])
    else:
        print("JSON list, first item:", json.dumps(data[0], indent=2)[:800] if data else "[]")


if __name__ == "__main__":
    main()
