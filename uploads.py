"""Uploads and downloads: size limits, StoredFile rows, and sending files safely.

storage.py moves the bytes; this module decides whether an upload is
allowed, records it in the StoredFile table, and sends files back to
browsers with safe headers.
"""

import csv
import html
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel import Session, func, select

import storage
from db import InboxFile, StoredFile
from web import human_size, templates

MB = 1024 * 1024
COURSEWORK_FILE_LIMIT = 50 * MB  # assignment, exam and submission files
AGENT_FILE_LIMIT = 5 * MB  # agent output files and monitor PDFs
INBOX_FILE_LIMIT = 50 * MB  # one Inbox file
INBOX_TOTAL_LIMIT = 200 * MB  # all waiting Inbox files together, on every agent
STORAGE_TOTAL_LIMIT = 900 * MB  # Supabase's free plan has 1 GB; keep some room
MAX_FILES_PER_ITEM = 20  # files on one assignment, exam or submission

# Only these are ever shown inside a page. Everything else (including SVG,
# HTML and PDF from members, which could run scripts or fake a page) is
# download-only.
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


# Files that can be previewed inside the page (besides images, shown directly):
# PDFs in the browser's PDF viewer, and HTML and plain text in a fully
# sandboxed frame (see preview_response()).
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".py", ".yaml", ".yml"}
HTML_EXTENSIONS = {".html", ".htm"}
PREVIEW_LIMIT = 20 * MB  # bigger files are only offered as downloads

# The rules for showing a text file from a member: "sandbox" makes the browser
# treat it as coming from a separate, unknown site (so it can't see anyone's
# sign-in or use this site), and scripts, forms and pop-ups are off. Only
# styles, pictures and fonts written inside the file itself are allowed.
SANDBOX_POLICY = ("sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
                  "frame-ancestors 'self'")
# HTML pages (e.g. an agent's monitor.html with charts) may also run the
# scripts written inside them ("allow-scripts"), still as that separate,
# unknown site: they can't see anyone's sign-in or this site's pages, and
# "default-src 'none'" means they can't load or send anything over the
# internet either. Only pages whose scripts are inside the file itself work.
HTML_POLICY = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
               "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; "
               "frame-ancestors 'self'")
CSV_EXTENSIONS = {".csv", ".tsv"}
CSV_ROWS = 2000  # a preview shows at most this many rows


def preview_kind(stored: StoredFile | None) -> str | None:
    """How a file can be previewed in the page: "pdf", "html", "text", or None."""
    if stored is None or stored.size > PREVIEW_LIMIT:
        return None
    name = stored.filename.lower()
    extension = name[name.rfind("."):] if "." in name else ""
    if stored.content_type == "application/pdf" or extension == ".pdf":
        return "pdf"
    if stored.content_type == "text/html" or extension in HTML_EXTENSIONS:
        return "html"
    if extension in CSV_EXTENSIONS or stored.content_type in ("text/csv", "text/tab-separated-values"):
        return "csv"
    if extension == ".json" or stored.content_type == "application/json":
        return "json"
    if stored.content_type.startswith("text/") or extension in TEXT_EXTENSIONS:
        return "text"
    return None


# Google Drive / Docs links that can be shown with Google's own preview.
DRIVE_PATTERNS = [
    (re.compile(r"^https://drive\.google\.com/file/d/([\w-]+)"), "https://drive.google.com/file/d/{}/preview"),
    (re.compile(r"^https://drive\.google\.com/open\?id=([\w-]+)"), "https://drive.google.com/file/d/{}/preview"),
    (re.compile(r"^https://docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]+)"), None),
]


def drive_preview_url(url: str | None) -> str | None:
    """Google's embeddable preview of a Drive or Docs link, or None for any other link.
    (It shows only what the viewer's own Google account may open.)"""
    for pattern, template in DRIVE_PATTERNS:
        match = pattern.match((url or "").strip())
        if match:
            if template is None:
                return f"https://docs.google.com/{match.group(1)}/d/{match.group(2)}/preview"
            return template.format(match.group(1))
    return None


templates.env.globals["drive_preview_url"] = drive_preview_url


def csv_table_page(raw: bytes, filename: str) -> str:
    """A CSV or TSV file as an HTML table (every cell escaped), sortable by
    clicking a column's heading. At most CSV_ROWS rows."""
    text = raw.decode("utf-8-sig", errors="replace")
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",\t;").delimiter
    except csv.Error:
        pass
    rows = []
    for number, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if number > CSV_ROWS:
            break
        rows.append(row)
    head, body = (rows[0], rows[1:]) if rows else ([], [])
    cut = len(body) >= CSV_ROWS
    cell = lambda value: html.escape(value)
    return ("<!doctype html><meta charset='utf-8'><style>"
            "body{font:13px/1.4 system-ui,sans-serif;margin:0;color:#0b1f3a}"
            "table{border-collapse:collapse;width:max-content;min-width:100%}"
            "th,td{border:1px solid #c9d5e6;padding:4px 8px;text-align:left;white-space:nowrap}"
            "th{position:sticky;top:0;background:#d7e5f8;cursor:pointer;user-select:none}"
            "th:after{content:' \\2195';opacity:.4}tr:nth-child(even) td{background:#f4f8fd}"
            "p{margin:6px 8px;color:#55657c}</style>"
            "<table><thead><tr>" + "".join(f"<th>{cell(h)}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell(v)}</td>" for v in row) + "</tr>" for row in body)
            + "</tbody></table>"
            + (f"<p>Showing the first {CSV_ROWS:,} rows; download the file for the rest.</p>" if cut else "")
            + "<script>document.querySelectorAll('th').forEach(function(th,i){var up=true;th.onclick=function(){"
              "var tb=document.querySelector('tbody'),rows=[].slice.call(tb.rows);rows.sort(function(a,b){"
              "var x=a.cells[i]?a.cells[i].textContent:'',y=b.cells[i]?b.cells[i].textContent:'',"
              "p=parseFloat(x),q=parseFloat(y);var r=(!isNaN(p)&&!isNaN(q))?p-q:x.localeCompare(y);"
              "return up?r:-r});up=!up;rows.forEach(function(r){tb.appendChild(r)})}})</script>")


templates.env.globals["preview_kind"] = preview_kind


class UploadRefused(Exception):
    """An upload that can't be accepted; the message says why."""


def upload_size(upload: UploadFile) -> int:
    """How big an uploaded file is. FastAPI has already saved it to a
    temporary file, so we just look at where its end is."""
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def has_file(upload: UploadFile | None) -> bool:
    """True if a file was actually chosen (forms send an empty part otherwise)."""
    return upload is not None and bool(upload.filename)


def chosen_files(uploads: list[UploadFile] | None) -> list[UploadFile]:
    """The files actually chosen in a multi-file field (forms send an empty part otherwise)."""
    return [upload for upload in uploads or [] if has_file(upload)]


def total_stored(db: Session) -> int:
    return db.exec(select(func.coalesce(func.sum(StoredFile.size), 0))).one()


def inbox_waiting_total(db: Session) -> int:
    statement = select(func.coalesce(func.sum(StoredFile.size), 0)).join(
        InboxFile, InboxFile.file_id == StoredFile.id
    )
    return db.exec(statement).one()


def save_upload(db: Session, upload: UploadFile, limit: int, uploaded_by: int | None,
                extra_total: tuple[int, int] | None = None) -> StoredFile:
    """Check an upload against the limits, store it, and add its StoredFile row.

    limit:       the largest file allowed, in bytes.
    extra_total: (bytes already used, cap) for an extra limit, e.g. the Inbox.
    The caller commits. Raises UploadRefused (with nothing stored).
    """
    name = Path(upload.filename or "file").name[:200] or "file"
    size = upload_size(upload)
    if size > limit:
        raise UploadRefused(f"{name} is too big: files can be up to {human_size(limit)}.")
    if total_stored(db) + size > STORAGE_TOTAL_LIMIT:
        raise UploadRefused(
            "The site's file storage is nearly full, so new uploads are switched off. "
            "Please ask an admin to make room."
        )
    if extra_total is not None and extra_total[0] + size > extra_total[1]:
        raise UploadRefused(
            f"The Inbox is full: waiting files can take up to {human_size(extra_total[1])} in all. "
            "Wait for agents to take theirs, or remove some."
        )
    content_type = (upload.content_type or "application/octet-stream")[:100]
    try:
        path = storage.save(upload.file, content_type)
    except storage.StorageError as error:
        raise UploadRefused(str(error)) from error
    stored = StoredFile(storage_path=path, filename=name, content_type=content_type, size=size,
                        uploaded_by=uploaded_by)
    db.add(stored)
    db.flush()  # gives it an id
    return stored


def save_uploads(db: Session, uploads: list[UploadFile], limit: int, uploaded_by: int | None) -> list[StoredFile]:
    """save_upload() for several files: all of them, or none.

    If one is refused, the ones already stored are removed from storage again
    (the caller rolls back their rows) and UploadRefused is raised.
    """
    total = sum(upload_size(upload) for upload in uploads)
    if total_stored(db) + total > STORAGE_TOTAL_LIMIT:
        raise UploadRefused(
            "The site's file storage is nearly full, so new uploads are switched off. "
            "Please ask an admin to make room."
        )
    saved: list[StoredFile] = []
    try:
        for upload in uploads:
            saved.append(save_upload(db, upload, limit, uploaded_by))
    except UploadRefused:
        for stored in saved:
            storage.delete(stored.storage_path)
        raise
    return saved


def delete_stored_file(db: Session, file_id: int | None) -> None:
    """Delete a StoredFile row and its bytes in storage. The caller commits."""
    if not file_id:
        return
    stored = db.get(StoredFile, file_id)
    if stored is None:
        return
    storage.delete(stored.storage_path)
    db.delete(stored)


def _pieces(stored: StoredFile):
    """The file's bytes, a piece at a time. The first piece is fetched before
    the reply starts, so a missing file (or a storage problem) becomes a clear
    error message instead of a download that breaks halfway."""
    pieces = storage.open(stored.storage_path)
    try:
        first = next(pieces, b"")
    except storage.FileMissing:
        raise HTTPException(404, f"{stored.filename} is missing from file storage. It may have been lost "
                                 "before file storage was set up; ask its owner to upload it again.")
    except storage.StorageError as error:
        raise HTTPException(502, str(error))

    def stream():
        if first:
            yield first
        yield from pieces

    return stream()


def file_response(stored: StoredFile, inline: bool | None = None, filename: str | None = None,
                  media_type: str | None = None, cache: str = "private, no-cache") -> StreamingResponse:
    """Send a stored file to the browser, streamed from storage.

    Only PNG, JPEG, GIF and WebP may be shown inside the page; anything else
    is sent as a download ("attachment") with a neutral type, and nosniff
    stops the browser guessing a riskier type.
    """
    if inline is None:
        inline = stored.content_type in INLINE_TYPES
    if media_type is None:
        media_type = stored.content_type if inline else "application/octet-stream"
    name = filename or stored.filename
    return StreamingResponse(
        _pieces(stored),
        media_type=media_type,
        headers={
            # filename*= safely carries any file name, including spaces and Japanese.
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}",
            "Content-Length": str(stored.size),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": cache,
        },
    )


def preview_response(stored: StoredFile) -> StreamingResponse:
    """Send a file to be shown inside the page (the caller checks permissions).

    - PDF: only if it really is one (it starts with "%PDF-"), in the browser's
      own PDF viewer.
    - HTML and text: under SANDBOX_POLICY, so nothing in the file can run or
      reach this site; text is shown as plain text, never as HTML.
    """
    kind = preview_kind(stored)
    if kind is None:
        raise HTTPException(404, "This file can't be previewed; download it instead.")
    pieces = _pieces(stored)
    first = next(pieces, b"")
    if kind == "pdf" and not first.startswith(b"%PDF-"):
        raise HTTPException(404, "This file isn't a real PDF, so it can't be previewed; download it instead.")

    def stream():
        yield first
        yield from pieces

    headers = {"Content-Disposition": f"inline; filename*=UTF-8''{quote(stored.filename)}",
               "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-cache"}
    if kind == "pdf":
        return StreamingResponse(stream(), media_type="application/pdf", headers=headers)
    if kind == "csv":  # a sortable table (cells escaped; the only script is ours)
        headers["Content-Security-Policy"] = HTML_POLICY
        page = csv_table_page(b"".join(stream()), stored.filename)
        return StreamingResponse(iter([page.encode("utf-8")]), media_type="text/html; charset=utf-8", headers=headers)
    if kind == "json":  # laid out with indents, if it really is JSON; as it is otherwise
        headers["Content-Security-Policy"] = SANDBOX_POLICY
        raw = b"".join(stream())
        try:
            raw = json.dumps(json.loads(raw.decode("utf-8-sig")), indent=2, ensure_ascii=False).encode("utf-8")
        except ValueError:
            pass
        return StreamingResponse(iter([raw]), media_type="text/plain; charset=utf-8", headers=headers)
    headers["Content-Security-Policy"] = HTML_POLICY if kind == "html" else SANDBOX_POLICY
    media_type = "text/html; charset=utf-8" if kind == "html" else "text/plain; charset=utf-8"
    return StreamingResponse(stream(), media_type=media_type, headers=headers)
