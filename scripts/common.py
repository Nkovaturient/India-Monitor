#!/usr/bin/env python3
"""Shared HTTP + Supabase helpers for India Monitor pipeline scripts."""

import datetime as dt
import json
import os
import time

import requests

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


def log(msg):
    print(f"[{dt.datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def supabase_config():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    return url, key


def _sb_headers():
    _, key = supabase_config()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def supabase_get(table, params=""):
    url, _ = supabase_config()
    endpoint = f"{url}/rest/v1/{table}"
    if params:
        endpoint += f"?{params}"
    resp = requests.get(endpoint, headers=_sb_headers(), timeout=30)
    if resp.status_code != 200:
        log(f"  ! read from {table} failed: {resp.status_code} {resp.text[:300]}")
        return []
    try:
        return resp.json()
    except Exception:
        return []


def supabase_post(table, rows, on_conflict=None, prefer_extra="", merge=False):
    if not rows:
        return 0
    url, _ = supabase_config()
    endpoint = f"{url}/rest/v1/{table}"
    prefer = "return=representation"
    if on_conflict:
        endpoint += f"?on_conflict={on_conflict}"
        resolution = "resolution=merge-duplicates" if merge else "resolution=ignore-duplicates"
        prefer = f"{resolution},{prefer}"
    if prefer_extra:
        prefer = f"{prefer_extra},{prefer}"
    headers = {**_sb_headers(), "Prefer": prefer}
    resp = requests.post(endpoint, headers=headers, data=json.dumps(rows), timeout=30)
    if resp.status_code not in (200, 201):
        log(f"  ! write to {table} failed: {resp.status_code} {resp.text[:300]}")
        return 0
    try:
        return len(resp.json())
    except Exception:
        return 0


def supabase_patch(table, query, payload):
    url, _ = supabase_config()
    endpoint = f"{url}/rest/v1/{table}?{query}"
    headers = {**_sb_headers(), "Prefer": "return=representation"}
    resp = requests.patch(endpoint, headers=headers, data=json.dumps(payload), timeout=30)
    if resp.status_code not in (200, 204):
        log(f"  ! patch {table} failed: {resp.status_code} {resp.text[:300]}")
        return 0
    if resp.status_code == 204 or not resp.text:
        return 1
    try:
        data = resp.json()
        return len(data) if isinstance(data, list) else 1
    except Exception:
        return 1


def record_health(source, ok, items_seen=0, items_kept=0, error=None):
    supabase_post("pipeline_health", [{
        "source": source, "ok": ok, "items_seen": items_seen,
        "items_kept": items_kept, "error": (error or "")[:500],
    }])


def fetch_url(url, attempts=2, accept=None, timeout=20):
    """Fetch raw bytes with browser-like headers + retries. Returns bytes or raises."""
    last_err = None
    headers = dict(FETCH_HEADERS)
    if accept:
        headers["Accept"] = accept
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.content:
                return resp.content
            last_err = f"HTTP {resp.status_code}, {len(resp.content)} bytes"
        except Exception as e:
            last_err = str(e)
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise RuntimeError(last_err or "unknown fetch failure")
