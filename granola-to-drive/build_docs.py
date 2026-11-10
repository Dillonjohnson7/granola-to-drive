#!/usr/bin/env python3
"""
build_docs.py — turn the raw Granola export into per-meeting dossiers ready for Google Drive.

Reads:
  ~/Library/Application Support/Granola/granola_export/
    documents.json
    panels/<id>.json
    transcripts/<id>.json

Writes:
  ~/Library/Application Support/Granola/granola_export/built/
    <date>_<id>.txt        — plain-text dossier (title, date, people, summary, notes, transcript)
    manifest.json          — index of [{id, title, date, drive_title, path}]
"""

import json
import re
import unicodedata
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import os

DEFAULT_ROOT = Path.home() / "Library/Application Support/Granola/granola_export"
ROOT = Path(os.environ.get("GRANOLA_EXPORT_DIR", str(DEFAULT_ROOT)))
OUT = ROOT / "built"
SEP = "=" * 64


class _HTMLToText(HTMLParser):
    """Minimal HTML→text — preserves line breaks for <li>, <p>, <h*>, <br>."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.list_depth = 0
        self.in_li = False
        self.ordered_stack: list[bool] = []
        self.li_counters: list[int] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl()
            if t.startswith("h"):
                self._emit("\n")
        elif t in ("br",):
            self._emit("\n")
        elif t in ("ul", "ol"):
            self.list_depth += 1
            self.ordered_stack.append(t == "ol")
            self.li_counters.append(0)
            self._nl()
        elif t == "li":
            self.in_li = True
            indent = "  " * (self.list_depth - 1) if self.list_depth else ""
            if self.ordered_stack and self.ordered_stack[-1]:
                self.li_counters[-1] += 1
                self._emit(f"\n{indent}{self.li_counters[-1]}. ")
            else:
                self._emit(f"\n{indent}- ")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n")
        elif t in ("ul", "ol"):
            self.list_depth = max(0, self.list_depth - 1)
            if self.ordered_stack:
                self.ordered_stack.pop()
            if self.li_counters:
                self.li_counters.pop()
            self._emit("\n")
        elif t == "li":
            self.in_li = False

    def handle_data(self, data):
        self._emit(data)

    def _emit(self, s):
        self.parts.append(s)

    def _nl(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self._emit("\n")

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = unescape(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _HTMLToText()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", "", html)
    return p.get_text()


def fmt_date_human(iso: str) -> str:
    """ISO 8601 -> 'Apr 26, 2026 4:03 PM' (UTC if no tz info)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.strftime("%b %-d, %Y %-I:%M %p")


def fmt_date_short(iso: str) -> str:
    if not iso:
        return "0000-00-00"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10]


def fmt_date_filename(iso: str) -> str:
    """Filename-friendly date: YYYY-MMM-DD (e.g. 2026-Apr-26)."""
    if not iso:
        return "0000-Jan-01"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%b-%d")
    except ValueError:
        return iso[:10]


def fmt_timestamp(seg_ts: str, base: datetime | None) -> str:
    """Convert a segment ISO timestamp into HH:MM:SS offset from base, or wall time."""
    try:
        t = datetime.fromisoformat(seg_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    if base:
        delta = (t - base).total_seconds()
        if delta < 0:
            delta = 0
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        s = int(delta % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return t.strftime("%H:%M:%S")


def participants_str(doc: dict) -> str:
    people = doc.get("people") or {}
    out = []
    creator = people.get("creator")
    if isinstance(creator, dict):
        name = (
            (creator.get("details") or {}).get("person", {}).get("name", {}).get("fullName")
            or creator.get("name")
            or creator.get("email")
        )
        email = creator.get("email")
        if name and email:
            out.append(f"{name} <{email}>")
        elif email:
            out.append(email)
    for a in people.get("attendees") or []:
        if not isinstance(a, dict):
            continue
        name = (a.get("details") or {}).get("person", {}).get("name", {}).get("fullName")
        email = a.get("email")
        if name and email:
            entry = f"{name} <{email}>"
        elif email:
            entry = email
        else:
            continue
        if entry not in out:
            out.append(entry)
    return ", ".join(out) if out else "(no participants listed)"


def summary_text(doc_id: str) -> str:
    """Best-effort extract of the AI summary from panels/<id>.json."""
    p = ROOT / "panels" / f"{doc_id}.json"
    if not p.exists():
        return ""
    try:
        panels = json.loads(p.read_text())
    except Exception:
        return ""
    if not isinstance(panels, list) or not panels:
        return ""
    # Prefer most recently updated, non-deleted panel
    panels = [pp for pp in panels if isinstance(pp, dict) and not pp.get("deleted_at")]
    panels.sort(key=lambda pp: pp.get("content_updated_at") or pp.get("updated_at") or "", reverse=True)
    for panel in panels:
        html = panel.get("original_content")
        if html:
            text = html_to_text(html)
            if text.strip():
                return text
    return ""


def transcript_text(doc_id: str, base_iso: str | None) -> str:
    p = ROOT / "transcripts" / f"{doc_id}.json"
    if not p.exists():
        return "(no transcript available)"
    try:
        segs = json.loads(p.read_text())
    except Exception:
        return "(transcript file unreadable)"
    if not isinstance(segs, list) or not segs:
        return "(transcript empty)"

    base = None
    if base_iso:
        try:
            base = datetime.fromisoformat(base_iso.replace("Z", "+00:00"))
        except ValueError:
            base = None
    if base is None and segs:
        first_ts = segs[0].get("start_timestamp")
        try:
            base = datetime.fromisoformat(first_ts.replace("Z", "+00:00")) if first_ts else None
        except Exception:
            base = None

    lines = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        ts = fmt_timestamp(s.get("start_timestamp", ""), base)
        speaker = s.get("source") or s.get("speaker") or ""
        if speaker == "system":
            speaker = "Host"
        elif speaker == "microphone":
            speaker = "You"
        prefix = f"[{ts}] {speaker}: " if ts else f"{speaker}: " if speaker else ""
        lines.append(f"{prefix}{text}")
    return "\n".join(lines) if lines else "(transcript empty)"


def _sanitize_filename(name: str) -> str:
    """macOS-safe filename. Strips slashes, collapses whitespace, trims trailing dots."""
    name = name.replace("/", "-").replace("\x00", "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(".").strip()
    return name or "untitled"


def without_header_tags(text: str) -> str:
    """Ignore the tagger's header line when comparing rendered meeting content."""
    header, separator, rest = text.partition("\n\n")
    header = re.sub(r"(?m)^(Granola Meeting ID:[^\n]*)\nTags:[^\n]*", r"\1", header, count=1)
    return header + separator + rest


def write_dossier(path: Path, body: str) -> bool:
    """Keep existing tags and mtime when the source content is unchanged."""
    if path.exists() and without_header_tags(path.read_text()) == body:
        return False
    path.write_text(body)
    return True


def build_one(doc: dict) -> tuple[str, str, str]:
    doc_id = doc["id"]
    raw_title = (doc.get("title") or "(untitled)").strip()
    date_iso = doc.get("created_at") or doc.get("updated_at") or ""
    file_date = fmt_date_filename(date_iso)
    drive_title = f"{_sanitize_filename(raw_title)}, {file_date}"
    fs_name = drive_title + ".txt"

    summary = summary_text(doc_id) or "(no summary available)"
    notes = (doc.get("notes_plain") or doc.get("notes_markdown") or "").strip() or "(no private notes)"
    transcript = transcript_text(doc_id, date_iso)
    participants = participants_str(doc)

    body = "\n".join(
        [
            raw_title,
            f"Date: {fmt_date_human(date_iso)}",
            f"Granola Meeting ID: {doc_id}",
            f"Participants: {participants}",
            "",
            SEP,
            "AI SUMMARY",
            SEP,
            "",
            summary,
            "",
            SEP,
            "PRIVATE NOTES",
            SEP,
            "",
            notes,
            "",
            SEP,
            "FULL TRANSCRIPT",
            SEP,
            "",
            transcript,
            "",
            "---",
            "Synced from Granola via Cowork.",
        ]
    )
    return fs_name, drive_title, body


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    docs = json.loads((ROOT / "documents.json").read_text())
    manifest = []
    changed = 0
    used_paths: dict[str, str] = {}  # filename -> doc_id of file that took the slot
    for doc in docs:
        if doc.get("deleted_at") or doc.get("was_trashed"):
            continue
        fs_name, drive_title, body = build_one(doc)
        # macOS commonly treats case and Unicode normalization as equivalent.
        stem, ext = fs_name.rsplit(".", 1)
        suffix = 0
        key = unicodedata.normalize("NFD", fs_name).casefold()
        while key in used_paths and used_paths[key] != doc["id"]:
            label = doc["id"][:8] + (f"-{suffix}" if suffix else "")
            fs_name = f"{stem} [{label}].{ext}"
            key = unicodedata.normalize("NFD", fs_name).casefold()
            suffix += 1
        used_paths[key] = doc["id"]
        path = OUT / fs_name
        if write_dossier(path, body):
            changed += 1
        manifest.append(
            {
                "id": doc["id"],
                "title": doc.get("title"),
                "date": doc.get("created_at"),
                "drive_title": drive_title,
                "path": str(path),
                "filename": fs_name,
                "size": len(body),
            }
        )

    manifest.sort(key=lambda m: m["date"] or "", reverse=True)
    manifest_path = OUT / "manifest.json"
    manifest_text = json.dumps(manifest, indent=2)
    if not manifest_path.exists() or manifest_path.read_text() != manifest_text:
        manifest_path.write_text(manifest_text)
    print(f"Built {len(manifest)} dossiers ({changed} changed, {len(manifest)-changed} unchanged) into {OUT}")
    print(f"Manifest: {OUT/'manifest.json'}")


if __name__ == "__main__":
    main()
