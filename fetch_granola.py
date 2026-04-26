#!/usr/bin/env python3
"""
fetch_granola.py — pull every Granola meeting (full transcript + AI panels)
using the local desktop-app token in ~/Library/Application Support/Granola/supabase.json.

Why this exists: Granola's official MCP gates transcripts behind a paid plan,
but the desktop app fetches them via an internal Bearer-auth API using a
WorkOS access_token already on disk. We just reuse that.

Usage:
    python3 fetch_granola.py

Output (written to ~/Library/Application Support/Granola/granola_export/):
    documents.json          — one entry per meeting (title, date, notes, summary panels)
    transcripts/<id>.json   — list of transcript segments (text, timestamps, speaker)
    summary.txt             — quick console summary

Safe to re-run. Skips transcripts already on disk.
"""

import gzip
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error
import zlib
from pathlib import Path

SUPABASE = Path.home() / "Library/Application Support/Granola/supabase.json"
OUT = Path.home() / "Library/Application Support/Granola/granola_export"
TRANSCRIPTS_DIR = OUT / "transcripts"
PANELS_DIR = OUT / "panels"

API_BASE = "https://api.granola.ai"
UA = "Granola/6.4.0 (reverse-engineered local export)"


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_token():
    if not SUPABASE.exists():
        die(f"Granola credentials not found at {SUPABASE} — is the desktop app installed and signed in?")
    with open(SUPABASE) as f:
        sup = json.load(f)
    workos = json.loads(sup["workos_tokens"])
    obtained_at = workos["obtained_at"]
    expires_in_ms = workos["expires_in"] * 1000
    expiry = obtained_at + expires_in_ms
    now = int(time.time() * 1000)
    remaining = (expiry - now) / 1000
    if remaining < 60:
        die(
            f"WorkOS access token expires in {remaining:.0f}s. "
            "Open the Granola desktop app to trigger a refresh, then re-run."
        )
    print(f"Token OK ({remaining/60:.0f} min remaining).")
    return workos["access_token"]


def _decode_body(raw, content_encoding):
    if not raw:
        return None
    enc = (content_encoding or "").lower()
    if enc == "gzip" or (len(raw) >= 2 and raw[:2] == b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    elif enc == "br":
        try:
            import brotli  # type: ignore
            raw = brotli.decompress(raw)
        except Exception:
            raise RuntimeError("response is brotli-encoded but `brotli` not installed; pip install brotli")
    return json.loads(raw)


def post(path, token, body, timeout=30):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "User-Agent": UA,
            "X-Client-Version": "6.4.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            enc = r.headers.get("Content-Encoding")
            return r.status, _decode_body(raw, enc)
    except urllib.error.HTTPError as e:
        raw = e.read()
        enc = e.headers.get("Content-Encoding") if hasattr(e, "headers") else None
        try:
            body = _decode_body(raw, enc)
        except Exception:
            body = raw.decode("utf-8", errors="replace")[:500]
        return e.code, {"_error": body}


def fetch_documents(token):
    """Pull all meeting documents.

    Granola's /v2/get-documents doesn't return a pagination cursor, but it
    *does* honor a high `limit` and supports `offset`. So we ask for a big
    page first; if it looks like we hit the cap (returned exactly PAGE),
    we keep paging via offset until a short page comes back.
    """
    PAGE = 1000  # Granola happily returns more than 100 if you ask
    all_docs = []
    offset = 0
    for _ in range(50):  # hard cap (50 pages = 50k meetings)
        body = {"limit": PAGE, "offset": offset}
        status, resp = post("/v2/get-documents", token, body)
        if status != 200:
            print(f"  /v2/get-documents failed ({status}): {resp}", file=sys.stderr)
            status, resp = post("/v1/get-documents", token, body)
            if status != 200:
                die(f"both /v1 and /v2 get-documents failed: {resp}")
        docs = resp.get("docs") or resp.get("documents") or []
        if not docs:
            break
        all_docs.extend(docs)
        print(f"  fetched {len(all_docs)} so far (page returned {len(docs)})...")
        if len(docs) < PAGE:
            break  # short page = end of data
        offset += len(docs)
        time.sleep(0.2)
    return all_docs


def fetch_transcript(token, doc_id):
    status, resp = post("/v1/get-document-transcript", token, {"document_id": doc_id})
    if status != 200:
        return None, f"HTTP {status}: {str(resp)[:200]}"
    return resp, None


def fetch_panels(token, doc_id):
    """AI summary panels (the structured summary you see in the Granola UI)."""
    status, resp = post("/v1/get-document-panels", token, {"document_id": doc_id})
    if status != 200:
        return None, f"HTTP {status}: {str(resp)[:200]}"
    return resp, None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    PANELS_DIR.mkdir(exist_ok=True)

    token = load_token()

    print("\nFetching documents…")
    docs = fetch_documents(token)
    print(f"Got {len(docs)} documents.\n")

    with open(OUT / "documents.json", "w") as f:
        json.dump(docs, f, indent=2, default=str)

    success, skip, fail = 0, 0, 0
    failures = []

    for i, doc in enumerate(docs, 1):
        doc_id = doc.get("id") or doc.get("document_id")
        title = (doc.get("title") or "(untitled)").strip()[:60]
        if not doc_id:
            print(f"[{i}/{len(docs)}] {title!r}: no id, skipping")
            fail += 1
            continue

        t_path = TRANSCRIPTS_DIR / f"{doc_id}.json"
        p_path = PANELS_DIR / f"{doc_id}.json"

        # Transcript
        if t_path.exists():
            t_status = "cached"
            skip += 1
        else:
            t, err = fetch_transcript(token, doc_id)
            if err:
                t_status = f"FAIL ({err})"
                fail += 1
                failures.append((doc_id, title, err))
            else:
                with open(t_path, "w") as f:
                    json.dump(t, f, indent=2, default=str)
                segs = len(t) if isinstance(t, list) else "?"
                t_status = f"{segs} segs"
                success += 1

        # Panels (best-effort)
        if not p_path.exists():
            p, _err = fetch_panels(token, doc_id)
            if p is not None:
                with open(p_path, "w") as f:
                    json.dump(p, f, indent=2, default=str)

        print(f"[{i}/{len(docs)}] {title}: {t_status}")
        time.sleep(0.3)  # gentle on the API

    print(f"\nDone. transcripts: {success} new, {skip} cached, {fail} failed.")
    print(f"Output: {OUT}")
    if failures:
        print("\nFailures:")
        for fid, ftitle, ferr in failures[:20]:
            print(f"  {fid} {ftitle!r}: {ferr}")

    with open(OUT / "summary.txt", "w") as f:
        f.write(f"Granola export at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Documents: {len(docs)}\n")
        f.write(f"Transcripts new: {success}, cached: {skip}, failed: {fail}\n")


if __name__ == "__main__":
    main()
