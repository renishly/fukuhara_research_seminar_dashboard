"""File storage: where uploaded files really live.

Files never go in the database (Supabase's free database is only 500 MB and
turns read-only when full). Instead:
  - On the deployed site, with SUPABASE_URL and SUPABASE_SERVICE_KEY set,
    files go into a *private* Supabase Storage bucket (SUPABASE_BUCKET,
    default "seminar-files"), through Supabase's Storage REST API.
  - Otherwise (on your computer, and in the tests) they go into a local
    folder: ./local_files/, or LOCAL_FILES_DIR if that is set.

The rest of the site only uses three functions:
    save(upload)  -> the storage path of the new file
    open(path)    -> the file's bytes, a piece at a time (for streaming)
    delete(path)
Browsers never see storage paths or Supabase addresses: every download goes
through the site's own /files/{id}, which checks permissions first.
"""

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

import requests

APP_DIR = Path(__file__).resolve().parent
CHUNK_BYTES = 1024 * 1024  # files are read and sent 1 MB at a time
TIMEOUT_SECONDS = 120  # a 50 MB upload can take a while


class StorageError(Exception):
    """File storage couldn't be reached or refused the request."""


class FileMissing(StorageError):
    """The file isn't in file storage (any more)."""


def _project_url(raw: str) -> str:
    """The project's base address, e.g. https://abcd.supabase.co. People often
    copy a longer address from Supabase's pages (ending in /rest/v1 or
    /storage/v1), so those endings are removed."""
    url = raw.strip().rstrip("/")
    for ending in ("/rest/v1", "/storage/v1", "/auth/v1"):
        if url.endswith(ending):
            url = url[: -len(ending)]
    return url


def _supabase() -> tuple[str, str, str] | None:
    """(url, service key, bucket) when Supabase Storage is configured, else None."""
    url = _project_url(os.getenv("SUPABASE_URL") or "")
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return None
    bucket = (os.getenv("SUPABASE_BUCKET") or "seminar-files").strip()
    return url, key, bucket


def _headers(key: str) -> dict:
    """How to send the service key. It can read every private file, so it is
    only ever used here, on the server.

    Newer Supabase secret keys ("sb_secret_...") go only in the "apikey"
    header: they aren't JWTs, so sending one as a bearer token fails. Older
    "service_role" keys are JWTs (they start with "eyJ") and are also sent as
    a bearer token, as Supabase expects for those.
    """
    if key.startswith("eyJ"):
        return {"apikey": key, "Authorization": f"Bearer {key}"}
    return {"apikey": key}


def local_dir() -> Path:
    """The folder used when Supabase isn't configured (read each time, so tests can change it)."""
    return Path(os.getenv("LOCAL_FILES_DIR") or APP_DIR / "local_files")


def using_supabase() -> bool:
    return _supabase() is not None


def on_render() -> bool:
    """True on Render, which sets RENDER=true for every service."""
    return (os.getenv("RENDER") or "").lower() == "true"


def _refusal(what: str, response: requests.Response) -> StorageError:
    """An error that says why Supabase refused, using its own message, e.g.
    'File storage refused the upload (HTTP 404: Bucket not found).'"""
    detail = ""
    try:
        body = response.json()
        detail = str(body.get("message") or body.get("error") or "")[:200]
    except ValueError:
        detail = response.text[:200]
    reason = f"HTTP {response.status_code}" + (f": {detail}" if detail else "")
    print(f"Storage: Supabase refused the {what} ({reason})")
    return StorageError(f"File storage refused the {what} ({reason}). Please tell an admin.")


def check() -> dict:
    """Where files are kept, and whether that works, for the Members page.

    Returns {"where": ..., "ok": True/False, "message": ...}. With Supabase,
    it asks for the bucket, which checks the address, the key and the
    bucket's name all at once (and changes nothing).
    """
    supabase = _supabase()
    if supabase is None:
        if on_render():
            return {"where": "this server's own disk", "ok": False,
                    "message": "SUPABASE_URL and SUPABASE_SERVICE_KEY aren't both set on Render, so files are "
                               "saved on the server's temporary disk and are lost whenever it restarts or sleeps. "
                               "Set them in the service's Environment tab."}
        return {"where": f"the local folder {local_dir()}", "ok": True, "message": "Files are kept on this computer."}
    url, key, bucket = supabase
    where = f"the Supabase bucket \"{bucket}\" at {url}"
    try:
        response = requests.get(f"{url}/storage/v1/bucket/{bucket}", headers=_headers(key), timeout=15)
    except requests.RequestException as error:
        return {"where": where, "ok": False, "message": f"Supabase couldn't be reached: {error}"}
    if response.ok:
        public = response.json().get("public") if "json" in response.headers.get("content-type", "") else None
        if public:
            return {"where": where, "ok": False,
                    "message": "Connected, but the bucket is PUBLIC: anyone with a file's address could read it. "
                               "Make it private in Supabase (Storage, bucket settings)."}
        return {"where": where, "ok": True, "message": "Connected: the address, key and bucket all work."}
    return {"where": where, "ok": False, "message": str(_refusal("connection check", response))
            .replace(" Please tell an admin.", "") + _hint(response.status_code)}


def _hint(status: int) -> str:
    if status in (400, 404):
        return " Check that the bucket exists and is named exactly as SUPABASE_BUCKET says."
    if status in (401, 403):
        return " Check SUPABASE_SERVICE_KEY: it must be the secret (service_role or sb_secret_) key, not the publishable/anon one."
    return ""


def new_path() -> str:
    """A fresh, unguessable storage path such as "2026/10/3f2a...e1".

    The uploaded file name is kept in the database instead, so odd characters
    in names never reach file storage.
    """
    now = datetime.now(timezone.utc)
    return f"{now:%Y/%m}/{uuid.uuid4().hex}"


def save(file: BinaryIO, content_type: str = "application/octet-stream") -> str:
    """Store a file and return its storage path.

    `file` is an open file (FastAPI's UploadFile.file). FastAPI has already
    spooled the upload to a temporary file on disk, so we stream it from
    there instead of reading it all into memory (Render's free service has
    little RAM).
    """
    path = new_path()
    file.seek(0)
    supabase = _supabase()
    if supabase is None:
        target = local_dir() / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            shutil.copyfileobj(file, out, CHUNK_BYTES)
        return path

    url, key, bucket = supabase
    try:
        # Passing the open file (not its bytes) makes `requests` stream it.
        response = requests.post(
            f"{url}/storage/v1/object/{bucket}/{path}",
            headers={**_headers(key), "Content-Type": content_type, "x-upsert": "false"},
            data=file,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise StorageError(f"File storage couldn't be reached: {error}") from error
    if not response.ok:
        raise _refusal("upload", response)
    return path


def open(path: str) -> Iterator[bytes]:  # noqa: A001 - "open" reads naturally here
    """The bytes of a stored file, one piece at a time (for a StreamingResponse).

    Careful: inside this file, open() now means *this* function, not Python's
    built-in one, which is why local files are opened with Path.open() here.
    """
    supabase = _supabase()
    if supabase is None:
        try:
            handle = (local_dir() / path).open("rb")
        except FileNotFoundError:
            raise FileMissing("This file is missing from file storage.") from None
        with handle:
            while piece := handle.read(CHUNK_BYTES):
                yield piece
        return

    url, key, bucket = supabase
    try:
        response = requests.get(
            f"{url}/storage/v1/object/{bucket}/{path}",
            headers=_headers(key),
            stream=True,  # pass the file on as it arrives, never all in memory
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise StorageError(f"File storage couldn't be reached: {error}") from error
    if response.status_code in (400, 404):
        raise FileMissing("This file is missing from file storage.")
    if not response.ok:
        raise _refusal("download", response)
    with response:
        yield from response.iter_content(chunk_size=CHUNK_BYTES)


def read_all(path: str) -> bytes:
    """A whole (small) stored file at once. Only used for files up to 5 MB."""
    return b"".join(open(path))


def delete(path: str) -> None:
    """Delete a stored file. A file that's already gone is not an error."""
    supabase = _supabase()
    if supabase is None:
        try:
            (local_dir() / path).unlink()
        except FileNotFoundError:
            pass
        return

    url, key, bucket = supabase
    try:
        requests.delete(
            f"{url}/storage/v1/object/{bucket}",
            headers=_headers(key),
            json={"prefixes": [path]},
            timeout=30,
        )
    except requests.RequestException as error:
        # The database row is deleted anyway; the file is just left over.
        print(f"Storage: couldn't delete {path}: {error}")
