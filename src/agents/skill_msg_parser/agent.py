"""
agent.py — MSG / Email Parser (direct mode, no LLM).

Parses .msg (Outlook CDFV2) or .eml (RFC 5322) files and writes a
structured JSON file with: sender, date, subject, body, attachments.

Dependencies:
  - extract-msg (already in requirements.txt) for .msg files.
  - Python stdlib `email` for .eml files.
"""
from __future__ import annotations

import email
import email.policy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# .msg extraction (via extract-msg)
# ---------------------------------------------------------------------------

def _parse_msg(path: Path) -> dict[str, Any]:
    """Parse an Outlook .msg file and return structured fields."""
    import extract_msg

    msg = extract_msg.Message(str(path))
    try:
        # Sender
        sender = msg.sender or ""

        # Date — extract-msg exposes a datetime or a string.
        raw_date = msg.date
        if isinstance(raw_date, datetime):
            date_str = raw_date.isoformat()
        elif raw_date:
            date_str = str(raw_date)
        else:
            date_str = ""

        # Subject
        subject = msg.subject or ""

        # Body — prefer plain text.
        body = msg.body or ""

        # Attachments
        attachments = []
        for att in msg.attachments:
            att_info: dict[str, Any] = {"filename": att.longFilename or att.shortFilename or "unnamed"}
            # Size: extract-msg stores attachment data; len gives bytes.
            try:
                att_info["size_bytes"] = len(att.data) if att.data else 0
            except Exception:
                att_info["size_bytes"] = 0
            attachments.append(att_info)

        return {
            "sender": sender,
            "date": date_str,
            "subject": subject,
            "body": body,
            "attachments": attachments,
        }
    finally:
        msg.close()


# ---------------------------------------------------------------------------
# .eml extraction (stdlib)
# ---------------------------------------------------------------------------

def _parse_eml(path: Path) -> dict[str, Any]:
    """Parse an RFC 5322 .eml file and return structured fields."""
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    # Sender
    sender = str(msg.get("From", ""))

    # Date
    date_header = msg.get("Date", "")
    date_str = ""
    if date_header:
        try:
            dt = email.utils.parsedate_to_datetime(str(date_header))
            date_str = dt.isoformat()
        except Exception:
            date_str = str(date_header)

    # Subject
    subject = str(msg.get("Subject", ""))

    # Body — walk MIME parts, prefer text/plain.
    body = ""
    html_body = ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain" and not body:
            payload = part.get_content()
            body = payload if isinstance(payload, str) else str(payload)
        elif ct == "text/html" and not html_body:
            payload = part.get_content()
            html_body = payload if isinstance(payload, str) else str(payload)
    if not body and html_body:
        # Crude HTML stripping — good enough for structured extraction.
        import re
        body = re.sub(r"<[^>]+>", "", html_body)
        body = body.strip()

    # Attachments
    attachments = []
    for part in msg.iter_attachments():
        filename = part.get_filename() or "unnamed"
        data = part.get_payload(decode=True)
        size = len(data) if data else 0
        attachments.append({"filename": filename, "size_bytes": size})

    return {
        "sender": sender,
        "date": date_str,
        "subject": subject,
        "body": body,
        "attachments": attachments,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    file_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Parse a .msg or .eml file and write the result as JSON.

    config_path and model_override are accepted for interface compatibility
    with the generic tab runner but are unused (no LLM involved).

    Args:
        file_path:   Path to the input .msg or .eml file.
        output_path: Where to write the output .json file.

    Returns:
        The JSON string (also written to *output_path*).
    """
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    ext = src.suffix.lower()
    print(f"[msg_parser] Parsing {src.name} ({ext}) …")

    if ext == ".msg":
        result = _parse_msg(src)
    elif ext == ".eml":
        result = _parse_eml(src)
    else:
        raise ValueError(
            f"Unsupported file type '{ext}'. Expected .msg or .eml."
        )

    # Write output.
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    json_str = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    out.write_text(json_str, encoding="utf-8")
    print(f"[msg_parser] Parsed email written to {out}")

    return json_str
