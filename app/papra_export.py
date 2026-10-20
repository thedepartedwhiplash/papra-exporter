#!/usr/bin/env python3
"""Bulk-exports documents from a self-hosted Papra instance.

Downloads the original file of every document in one or more Papra
organizations, optionally restricted to documents carrying certain tags.
Each download is verified against the SHA-256 hash recorded by Papra, and a
``manifest.json`` describing every exported document (tags, notes, dates,
custom properties, hash, saved path and status) is written to the output
directory.

The script is safe to re-run. Files that are already complete are skipped,
interrupted downloads never leave a truncated file under its final name, and
the manifest is merged by document ID rather than overwritten, so partial
runs (for example with ``--tag`` or ``--org``) do not erase earlier entries.
File modification times are set to the document dates unless
``--no-preserve-dates`` is given.

Requirements:
  Python 3.8 or later. Only the standard library is used.

  The API token needs the ``organizations:read`` and ``documents:read``
  permissions. A read-only token is recommended.

Typical usage example (PowerShell):

  $env:PAPRA_TOKEN = "your_api_token"
  python papra_export.py --url https://papra.example.com --out papra-export

If the server answers HTTP 403 even though the token is valid, a proxy or
firewall may be blocking Python's default User-Agent. The script sends
``papra-export/1.0`` instead; override it with ``--user-agent`` if needed.

Selecting documents by tag (case-insensitive; tag name or tag ID):

  python papra_export.py --url ... --tag invoice
  python papra_export.py --url ... --tag invoice --tag 2026 --match all

Narrowing the selection further (all filters combine with AND):

  python papra_export.py --url ... --since 2026-01-01 --until 2026-06-30
  python papra_export.py --url ... --type pdf --type "image/*"
  python papra_export.py --url ... --name "invoice-*"

Organizing the output and archiving it:

  python papra_export.py --url ... --layout year-month
  python papra_export.py --url ... --tag invoice --zip invoices.zip

Checking and repairing an earlier export:

  python papra_export.py --url ... --verify
  python papra_export.py --url ... --retry-failed

Listing the available tags with document counts, without downloading:

  python papra_export.py --url ... --list-tags

Output layout:

  <out>/<organization name>/[<layout folders>/]<document name>
  <out>/<organization name>/_trash/...   (only with --include-trash)
  <out>/manifest.json

Exit status:
  0 if every selected document was exported (or skipped as already present).
  Non-zero if the API could not be reached, no organization matched, or any
  download failed or did not match its recorded hash.
"""

import argparse
import fnmatch
import hashlib
import http.client
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, List, Optional, Set,
                    Tuple, TypeVar)

T = TypeVar("T")

# A document object as returned by the Papra API (parsed JSON).
Document = Dict[str, Any]
# One entry of manifest.json.
ManifestEntry = Dict[str, Any]

# Documents requested per list call.
PAGE_SIZE = 100
# Attempts per HTTP operation before giving up.
MAX_RETRIES = 5
# Bytes read per iteration while streaming a download to disk.
CHUNK_SIZE = 1 << 16
# Socket timeout in seconds.
TIMEOUT = 60
# Sent instead of urllib's "Python-urllib/3.x", which many proxies block.
DEFAULT_USER_AGENT = "papra-export/1.0"
# Folder layouts understood by ``plan_filenames``.
LAYOUTS = ("flat", "year", "year-month", "tag")
# Which date the date filters and the year layouts look at.
DATE_FIELDS = ("document", "created")
# Manifest statuses that ``--retry-failed`` downloads again.
RETRY_STATUSES = ("failed", "hash_mismatch")
# Names Windows refuses to use as a file name, with or without extension.
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

print_lock = threading.Lock()


def log(msg: str) -> None:
    """Prints a message to stdout in a thread-safe way.

    Args:
        msg: The text to print.
    """
    with print_lock:
        print(msg, flush=True)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def retry(fn: Callable[[], T]) -> T:
    """Calls ``fn`` and retries transient failures with backoff.

    Network errors, timeouts, HTTP 429 and HTTP 5xx responses are retried up
    to ``MAX_RETRIES`` attempts. A ``Retry-After`` header is honored when it
    holds a number of seconds; otherwise the wait doubles on each attempt.

    Args:
        fn: A zero-argument callable performing the HTTP operation.

    Returns:
        Whatever ``fn`` returns on its first successful call.

    Raises:
        urllib.error.HTTPError: On a non-retryable status (any 4xx other
            than 429), or when the final attempt fails with 429 or 5xx.
        urllib.error.URLError: If the final attempt fails with a network
            error. ``TimeoutError``, ``ConnectionError`` and
            ``http.client.HTTPException`` are re-raised the same way.
    """
    err: Exception
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise  # 4xx (auth, not found...) will not fix itself
            try:
                wait = int(e.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2 ** attempt
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException) as e:
            wait = 2 ** attempt
            err = e
        if attempt == MAX_RETRIES:
            raise err
        time.sleep(wait)
    raise AssertionError("unreachable: MAX_RETRIES must be at least 1")


def http_error_detail(e: urllib.error.HTTPError) -> str:
    """Summarizes an HTTP error, including the start of the response body.

    The body often says who refused the request (the Papra API, a reverse
    proxy or a firewall), which a bare status code does not.

    Args:
        e: The error raised by ``urllib``.

    Returns:
        A one-line description such as ``HTTP 403 Forbidden - <body>``.
    """
    try:
        body = e.read(300).decode("utf-8", errors="replace").strip()
    except OSError:
        body = ""
    body = " ".join(body.split())
    return f"HTTP {e.code} {e.reason}" + (f" - {body}" if body else "")


def make_request(base: str, token: str, path: str,
                 params: Optional[Dict[str, Any]] = None
                 ) -> urllib.request.Request:
    """Builds an authenticated GET request for the Papra API.

    Args:
        base: API base URL without a trailing slash, e.g.
            ``https://papra.example.com/api``.
        token: The API token, sent as a bearer token.
        path: Endpoint path starting with ``/``.
        params: Optional query-string parameters.

    Returns:
        A request object ready to pass to ``urllib.request.urlopen``.
    """
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"})


def get_json(base: str, token: str, path: str,
             params: Optional[Dict[str, Any]] = None) -> Any:
    """Fetches an API endpoint and parses the JSON response.

    Transient failures are retried as described in ``retry``.

    Args:
        base: API base URL without a trailing slash.
        token: The API token.
        path: Endpoint path starting with ``/``.
        params: Optional query-string parameters.

    Returns:
        The decoded JSON body.

    Raises:
        urllib.error.HTTPError: If the server returns an error status that
            is not retried, or retries are exhausted.
        urllib.error.URLError: If the server cannot be reached.
        ValueError: If the response body is not valid JSON.
    """
    req = make_request(base, token, path, params)

    def go() -> Any:
        """Performs a single fetch-and-parse attempt."""
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)

    return retry(go)


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------

def list_documents(base: str, token: str, org_id: str, trash: bool = False
                   ) -> Tuple[List[Document], Optional[int]]:
    """Lists every document of an organization, following pagination.

    Active documents are requested sorted by creation date so that pages
    stay stable while iterating. Duplicate IDs are removed defensively.

    Args:
        base: API base URL without a trailing slash.
        token: The API token.
        org_id: ID of the organization.
        trash: If True, list deleted documents (the trash) instead of
            active ones.

    Returns:
        A tuple ``(documents, reported_total)``. ``reported_total`` is the
        ``documentsCount`` the server reported, or None if it was absent.

    Raises:
        urllib.error.HTTPError: If the API returns an error status.
        urllib.error.URLError: If the server cannot be reached.
    """
    path = f"/organizations/{org_id}/documents" + (
        "/deleted" if trash else "")
    docs: List[Document] = []
    page = 0
    total: Optional[int] = None
    while True:
        params: Dict[str, Any] = {"pageIndex": page, "pageSize": PAGE_SIZE}
        if not trash:
            # Stable ordering so pages don't shuffle while we iterate.
            params.update(sortField="createdAt", sortOrder="asc")
        data = get_json(base, token, path, params)
        batch = data.get("documents", [])
        total = data.get("documentsCount", total)
        docs.extend(batch)
        if not batch or (total is not None and len(docs) >= total):
            break
        page += 1
    unique = {d["id"]: d for d in docs}  # de-dupe defensively
    return list(unique.values()), total


def filter_by_tags(docs: List[Document], wanted: Iterable[str],
                   match_all: bool) -> List[Document]:
    """Keeps only the documents that carry the wanted tags.

    A wanted value matches a document tag if it equals the tag's ID, name
    or normalized name, ignoring case and surrounding whitespace.

    Args:
        docs: Documents as returned by ``list_documents``.
        wanted: Tag names or tag IDs to look for.
        match_all: If True, a document must match every wanted value; if
            False, matching any one of them is enough.

    Returns:
        The matching documents, in their original order.
    """
    wanted_set = {w.strip().lower() for w in wanted}

    def keys(tag: Dict[str, Any]) -> set:
        """Returns the lowercase identifiers a tag can be matched by."""
        return {str(tag.get("id", "")).lower(),
                str(tag.get("name", "")).lower(),
                str(tag.get("normalizedName", "")).lower()}

    kept = []
    for d in docs:
        doc_keys = [keys(t) for t in d.get("tags", [])]
        # For each wanted value, does any of the document's tags match it?
        hits = [any(w in k for k in doc_keys) for w in wanted_set]
        if all(hits) if match_all else any(hits):
            kept.append(d)
    return kept


@dataclass
class ExportOptions:
    """What to export and how to save it.

    Attributes:
        tags: Only documents with these tags (names or IDs); empty means
            no tag filter.
        match_all: With several tags, require all of them instead of any.
        since: Only documents dated on or after this day.
        until: Only documents dated on or before this day.
        date_field: ``"document"`` for the document's own date (falling
            back to its upload date) or ``"created"`` for the upload date.
            Used by the date filters and the year layouts.
        mime_types: Only documents of these MIME types. Entries may use
            wildcards (``image/*``) or be a bare extension (``pdf``).
        names: Only documents whose name matches one of these patterns: a
            case-insensitive substring, or a wildcard pattern if it contains
            ``*``, ``?`` or ``[``.
        ids: Only documents with these IDs (a hand-picked selection).
        retry_failed: Only documents the manifest records as failed or as
            a hash mismatch; they are downloaded again unconditionally.
        include_trash: Also export trashed documents.
        id_prefix: Prefix file names with the document ID.
        layout: Folder layout inside each organization: ``"flat"``,
            ``"year"``, ``"year-month"`` or ``"tag"`` (the document's first
            tag alphabetically, ``_untagged`` if it has none).
        preserve_dates: Give saved files the document's date as their
            modification time.
        verify: Also hash files that are already on disk and download them
            again if they do not match.
        zip_path: If set, also write the documents of this run into a zip
            archive at this path.
        workers: Number of concurrent downloads.
    """

    tags: List[str] = field(default_factory=list)
    match_all: bool = False
    since: Optional[date] = None
    until: Optional[date] = None
    date_field: str = "document"
    mime_types: List[str] = field(default_factory=list)
    names: List[str] = field(default_factory=list)
    ids: List[str] = field(default_factory=list)
    retry_failed: bool = False
    include_trash: bool = False
    id_prefix: bool = False
    layout: str = "flat"
    preserve_dates: bool = True
    verify: bool = False
    zip_path: Optional[Path] = None
    workers: int = 4

    @property
    def has_filters(self) -> bool:
        """Whether any option narrows down the selection of documents."""
        return bool(self.tags or self.since or self.until or self.mime_types
                    or self.names or self.ids or self.retry_failed)

    def describe_filters(self) -> str:
        """Returns the active filters as one human-readable line."""
        parts = []
        if self.tags:
            joiner = " ALL " if self.match_all else " ANY "
            parts.append("tags " + joiner.join(self.tags))
        if self.since or self.until:
            span = f"{self.since or '...'} to {self.until or '...'}"
            parts.append(f"{span} ({self.date_field} date)")
        if self.mime_types:
            parts.append("type " + ", ".join(self.mime_types))
        if self.names:
            parts.append("name " + ", ".join(repr(n) for n in self.names))
        if self.ids:
            parts.append(f"{len(self.ids)} hand-picked")
        if self.retry_failed:
            parts.append("previously failed only")
        return "; ".join(parts) or "all documents"


def parse_iso(value: Any) -> Optional[datetime]:
    """Parses an ISO 8601 timestamp such as ``2026-10-12T05:00:00.000Z``.

    Args:
        value: The value from a document field; anything but a valid
            timestamp string yields None.

    Returns:
        A timezone-aware datetime (naive input is taken as UTC), or None.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def to_local(when: datetime) -> datetime:
    """Converts a datetime to the local timezone where the OS allows it."""
    try:
        return when.astimezone()
    except (OverflowError, OSError, ValueError):
        return when


def doc_date(doc: Document, which: str = "document") -> Optional[datetime]:
    """Returns the date of a document that filters and layouts use.

    Args:
        doc: The document.
        which: ``"created"`` for the upload date; anything else for the
            document's own date, falling back to the upload date.

    Returns:
        The date, or None if the document has no usable date.
    """
    created = parse_iso(doc.get("createdAt"))
    if which == "created":
        return created
    return parse_iso(doc.get("documentDate")) or created


def filter_by_date(docs: List[Document], since: Optional[date],
                   until: Optional[date], which: str = "document"
                   ) -> List[Document]:
    """Keeps the documents dated within a range of days.

    Days are judged in the local timezone, which is normally the one the
    dates were entered in. Documents without a date never match.

    Args:
        docs: The documents to filter.
        since: First day to keep, or None for no lower bound.
        until: Last day to keep (inclusive), or None for no upper bound.
        which: Date to use, as for ``doc_date``.

    Returns:
        The documents inside the range, in their original order.
    """
    kept = []
    for d in docs:
        when = doc_date(d, which)
        if when is None:
            continue
        day = to_local(when).date()
        if (since and day < since) or (until and day > until):
            continue
        kept.append(d)
    return kept


def filter_by_type(docs: List[Document], patterns: Iterable[str]
                   ) -> List[Document]:
    """Keeps the documents whose MIME type matches a pattern.

    Args:
        docs: The documents to filter.
        patterns: MIME types, optionally with wildcards (``image/*``).
            A value without ``/`` is treated as a file extension, so
            ``pdf`` means ``application/pdf``. Matching ignores case.

    Returns:
        The matching documents, in their original order.
    """
    wanted = []
    for pattern in patterns:
        pattern = pattern.strip().lower()
        if "/" not in pattern:
            guessed = mimetypes.guess_type("x." + pattern.lstrip("."))[0]
            pattern = guessed or pattern
        wanted.append(pattern)
    return [d for d in docs
            if any(fnmatch.fnmatchcase((d.get("mimeType") or "").lower(), w)
                   for w in wanted)]


def filter_by_name(docs: List[Document], patterns: Iterable[str]
                   ) -> List[Document]:
    """Keeps the documents whose name matches a pattern.

    Args:
        docs: The documents to filter.
        patterns: Case-insensitive substrings, or wildcard patterns if the
            entry contains ``*``, ``?`` or ``[`` (for example
            ``invoice-*.pdf``). A document matches if its name or its
            original name matches any entry.

    Returns:
        The matching documents, in their original order.
    """
    wanted = [p.strip().lower() for p in patterns]

    def matches(name: str, pattern: str) -> bool:
        """Matches one name against one pattern."""
        if any(c in pattern for c in "*?["):
            return fnmatch.fnmatchcase(name, pattern)
        return pattern in name

    kept = []
    for d in docs:
        names = [str(d.get(k) or "").lower() for k in ("name", "originalName")]
        if any(matches(n, w) for n in names for w in wanted):
            kept.append(d)
    return kept


def select_documents(docs: List[Document], options: ExportOptions
                     ) -> List[Document]:
    """Applies every filter of the options; they combine with AND.

    ``retry_failed`` is not handled here because it needs the manifest;
    ``run_export`` applies it.

    Args:
        docs: The documents to filter.
        options: The export options.

    Returns:
        The documents that pass all filters, in their original order.
    """
    if options.ids:
        wanted = set(options.ids)
        docs = [d for d in docs if d["id"] in wanted]
    if options.tags:
        docs = filter_by_tags(docs, options.tags, options.match_all)
    if options.since or options.until:
        docs = filter_by_date(docs, options.since, options.until,
                              options.date_field)
    if options.mime_types:
        docs = filter_by_type(docs, options.mime_types)
    if options.names:
        docs = filter_by_name(docs, options.names)
    return docs


def load_manifest(path: Path) -> Dict[str, ManifestEntry]:
    """Loads an existing manifest so partial runs can merge into it.

    Args:
        path: Location of ``manifest.json``.

    Returns:
        Existing manifest entries keyed by document ID. The result is empty
        if the file is missing, unreadable or malformed.
    """
    try:
        return {m["id"]: m
                for m in json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def failed_document_ids(manifest_path: Path) -> Set[str]:
    """Returns the IDs the manifest records as failed or hash-mismatched.

    Args:
        manifest_path: Location of ``manifest.json``.

    Returns:
        The IDs whose status is in ``RETRY_STATUSES``; empty if the
        manifest is missing or unreadable.
    """
    return {i for i, m in load_manifest(manifest_path).items()
            if m.get("status") in RETRY_STATUSES}


def default_zip_path(out: Path) -> Path:
    """Returns where ``--zip`` puts the archive by default.

    Args:
        out: The export folder.

    Returns:
        The export folder's absolute path with ``.zip`` appended.
    """
    return Path(str(out.resolve()) + ".zip")


# --------------------------------------------------------------------------
# File naming
# --------------------------------------------------------------------------

def sanitize(name: str, max_stem: int = 150) -> str:
    """Turns a document or organization name into a safe file name.

    Replaces characters that are illegal on Windows, strips leading and
    trailing dots and spaces, prefixes reserved device names such as
    ``CON``, and truncates the stem (never the extension).

    Args:
        name: The original name.
        max_stem: Maximum length of the part before the extension.

    Returns:
        A non-empty string that is safe to use as a file or folder name.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "unnamed"
    stem, ext = os.path.splitext(name)
    if stem.upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    return stem[:max_stem] + ext


def layout_folder(doc: Document, layout: str,
                  date_field: str = "document") -> Path:
    """Returns the sub-folder a document goes into.

    Args:
        doc: The document.
        layout: One of ``LAYOUTS``.
        date_field: Date used by the year layouts, as for ``doc_date``.

    Returns:
        A relative path; empty for ``"flat"``. Undated documents go into
        ``undated`` and untagged ones into ``_untagged``.
    """
    if layout in ("year", "year-month"):
        when = doc_date(doc, date_field)
        if when is None:
            return Path("undated")
        local = to_local(when)
        if layout == "year":
            return Path(str(local.year))
        return Path(str(local.year), f"{local.month:02d}")
    if layout == "tag":
        names = sorted((t["name"] for t in doc.get("tags", [])),
                       key=str.lower)
        return Path(sanitize(names[0])) if names else Path("_untagged")
    return Path()


def plan_filenames(docs: List[Document], id_prefix: bool,
                   layout: str = "flat", date_field: str = "document"
                   ) -> Dict[str, Path]:
    """Chooses a unique relative path for every document.

    The sanitized document name is used. A missing extension is guessed
    from the MIME type. If two documents would end up at the same path
    (compared case-insensitively), the later one gets the last six
    characters of its ID appended before the extension.

    Args:
        docs: The documents to name.
        id_prefix: If True, prefix every name with the document ID, which
            makes names unique by construction.
        layout: Folder layout, one of ``LAYOUTS``.
        date_field: Date used by the year layouts, as for ``doc_date``.

    Returns:
        A mapping of document ID to path relative to the output folder of
        its organization.
    """
    used = set()
    paths: Dict[str, Path] = {}
    for d in docs:
        name = d.get("name") or d.get("originalName") or d["id"]
        name = sanitize(name)
        if not os.path.splitext(name)[1] and d.get("mimeType"):
            name += mimetypes.guess_extension(d["mimeType"]) or ""
        if id_prefix:
            name = f"{d['id']}_{name}"
        folder = layout_folder(d, layout, date_field)
        rel = folder / name
        if str(rel).lower() in used:  # duplicate path: add short id suffix
            stem, ext = os.path.splitext(name)
            rel = folder / f"{stem}_{d['id'][-6:]}{ext}"
        used.add(str(rel).lower())
        paths[d["id"]] = rel
    return paths


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """Returns the hex SHA-256 of a file, reading it in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def apply_file_date(path: Path, doc: Document) -> None:
    """Sets a file's modification time to the document's date.

    Failures (for example dates the filesystem cannot store) are ignored,
    because the date is a convenience.

    Args:
        path: The saved file.
        doc: The document it belongs to.
    """
    when = doc_date(doc)
    if when is None:
        return
    try:
        stamp = when.timestamp()
        os.utime(path, (stamp, stamp))
    except (OSError, OverflowError, ValueError):
        pass


def download(base: str, token: str, org_id: str, doc: Document,
             dest: Path, verify: bool = False, force: bool = False,
             preserve_dates: bool = True) -> str:
    """Downloads one document file and verifies it.

    The file is streamed to ``<dest>.part`` while its SHA-256 is computed,
    then atomically renamed to ``dest``. The partial file is removed if
    anything goes wrong. If ``dest`` already exists with the expected size
    (and, with ``verify``, the expected hash) the download is skipped.

    Args:
        base: API base URL without a trailing slash.
        token: The API token.
        org_id: ID of the organization owning the document.
        doc: The document's metadata, as returned by ``list_documents``.
        dest: Where to save the file; missing folders are created.
        verify: Hash an existing file before trusting it.
        force: Download even if a matching file exists.
        preserve_dates: Set the file's modification time to the document's
            date, also for skipped files.

    Returns:
        ``"skipped"`` if a complete file was already present; ``"ok"`` if
        a new download matched the recorded hash (or no hash was
        recorded); ``"repaired"`` if the same, but an existing file that
        did not match was replaced; ``"hash_mismatch"`` if the file was
        saved but its SHA-256 did not match the hash recorded by Papra.

    Raises:
        urllib.error.HTTPError: If the API returns an error status that is
            not retried, or retries are exhausted.
        urllib.error.URLError: If the server cannot be reached.
        OSError: If the file cannot be written.
    """
    expected_size = doc.get("originalSize")
    expected_hash = doc.get("originalSha256Hash")
    dest.parent.mkdir(parents=True, exist_ok=True)

    existed = dest.exists() and not force
    if (existed and expected_size is not None
            and dest.stat().st_size == expected_size
            and (not verify or not expected_hash
                 or sha256_file(dest) == expected_hash)):
        if preserve_dates:
            apply_file_date(dest, doc)
        return "skipped"

    req = make_request(
        base, token, f"/organizations/{org_id}/documents/{doc['id']}/file")
    tmp = dest.with_name(dest.name + ".part")

    def go() -> str:
        """Performs one streamed download attempt; returns its SHA-256."""
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, \
                open(tmp, "wb") as f:
            while True:
                chunk = r.read(CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
        return h.hexdigest()

    try:
        digest = retry(go)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()

    if preserve_dates:
        apply_file_date(dest, doc)
    if expected_hash and digest != expected_hash:
        return "hash_mismatch"
    return "repaired" if existed else "ok"


def build_zip(zip_path: Path, out: Path, entries: List[ManifestEntry]
              ) -> int:
    """Writes the exported documents of a run into a zip archive.

    The archive is written to a temporary file and renamed when complete.
    It contains every entry whose download did not fail, stored under its
    path relative to ``out``, plus a ``manifest.json`` describing exactly
    the documents of this run.

    Args:
        zip_path: Where to create the archive; replaced if it exists.
        out: The export folder the entries' paths are relative to.
        entries: Manifest entries of this run.

    Returns:
        The number of documents added to the archive.

    Raises:
        OSError: If the archive cannot be written.
    """
    tmp = zip_path.with_name(zip_path.name + ".part")
    count = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1,
                             strict_timestamps=False) as zf:
            for entry in entries:
                source = out / entry["path"]
                if entry["status"] != "failed" and source.is_file():
                    zf.write(source, arcname=entry["path"])
                    count += 1
            zf.writestr("manifest.json",
                        json.dumps(entries, indent=2, ensure_ascii=False))
        os.replace(tmp, zip_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return count


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_date_arg(value: str) -> date:
    """Parses a ``YYYY-MM-DD`` command-line value.

    Args:
        value: The text typed by the user.

    Returns:
        The date.

    Raises:
        argparse.ArgumentTypeError: If the text is not a valid date.
    """
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; use YYYY-MM-DD") from None


def parse_args() -> argparse.Namespace:
    """Parses and validates the command-line arguments.

    ``--url`` and ``--token`` fall back to the ``PAPRA_URL`` and
    ``PAPRA_TOKEN`` environment variables.

    Returns:
        The parsed arguments.

    Raises:
        SystemExit: If ``--help`` is requested, an argument is invalid, or
            neither the URL nor the token is available.
    """
    p = argparse.ArgumentParser(
        description="Bulk-export documents from Papra.")
    p.add_argument("--url", default=os.environ.get("PAPRA_URL"),
                   help="Papra base URL (or env PAPRA_URL), "
                        "e.g. https://papra.example.com")
    p.add_argument("--token", default=os.environ.get("PAPRA_TOKEN"),
                   help="API token (or env PAPRA_TOKEN)")
    p.add_argument("--out", default="papra-export",
                   help="Output directory")
    p.add_argument("--org",
                   help="Only export this organization (ID or name)")
    p.add_argument("--tag", action="append", default=[], metavar="TAG",
                   help="Only export documents with this tag (name or ID). "
                        "Repeat for multiple tags")
    p.add_argument("--match", choices=["any", "all"], default="any",
                   help="With several --tag values: documents having ANY "
                        "(default) or ALL of them")
    p.add_argument("--list-tags", action="store_true",
                   help="Print tags with document counts and exit "
                        "(downloads nothing)")
    p.add_argument("--include-trash", action="store_true",
                   help="Also export trashed documents")
    p.add_argument("--id-prefix", action="store_true",
                   help="Prefix filenames with the document ID "
                        "(guarantees uniqueness)")
    p.add_argument("--workers", type=int, default=4,
                   help="Concurrent downloads (default 4)")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                   help="User-Agent header to send (default "
                        f"{DEFAULT_USER_AGENT!r}). Some proxies and "
                        "firewalls block Python's default one")
    p.add_argument("--since", type=parse_date_arg, metavar="YYYY-MM-DD",
                   help="Only documents dated on or after this day")
    p.add_argument("--until", type=parse_date_arg, metavar="YYYY-MM-DD",
                   help="Only documents dated on or before this day")
    p.add_argument("--date-field", choices=DATE_FIELDS, default="document",
                   help="Date used by --since, --until and the year "
                        "layouts: the document's own date (default; falls "
                        "back to the upload date) or the upload date")
    p.add_argument("--type", action="append", default=[], metavar="MIME",
                   help="Only documents of this MIME type; wildcards "
                        "(image/*) and bare extensions (pdf) work. Repeat "
                        "for several types")
    p.add_argument("--name", action="append", default=[],
                   metavar="PATTERN",
                   help="Only documents whose name contains this text, or "
                        "matches this wildcard pattern (invoice-*.pdf). "
                        "Repeat for several")
    p.add_argument("--retry-failed", action="store_true",
                   help="Only download again the documents that "
                        "manifest.json records as failed or as a hash "
                        "mismatch")
    p.add_argument("--layout", choices=LAYOUTS, default="flat",
                   help="Folder layout inside each organization (default "
                        "flat): by year, by year and month, or by first tag")
    p.add_argument("--no-preserve-dates", dest="preserve_dates",
                   action="store_false",
                   help="Do not set file modification times to the "
                        "document dates")
    p.add_argument("--verify", action="store_true",
                   help="Hash files that already exist and download them "
                        "again if they do not match (slower reruns)")
    p.add_argument("--zip", nargs="?", const="", default=None,
                   metavar="FILE",
                   help="Also write this run's documents into a zip "
                        "archive (default: <out>.zip)")
    args = p.parse_args()
    if not args.url or not args.token:
        p.error("--url and --token are required "
                "(or set PAPRA_URL / PAPRA_TOKEN)")
    if args.since and args.until and args.since > args.until:
        p.error("--since must not be later than --until")
    return args


@dataclass
class ExportResult:
    """Outcome of ``run_export``.

    Attributes:
        expected: Number of documents selected for export.
        ok: Documents downloaded and verified.
        skipped: Documents that were already present on disk.
        repaired: Documents whose existing file did not match and was
            replaced by a verified download.
        hash_mismatch: Documents saved whose SHA-256 differed from the hash
            recorded by Papra.
        failed: Documents that could not be downloaded.
        cancelled: True if the run was stopped through the cancel event.
        zip_path: The zip archive that was created, if any.
        bytes_total: Combined size of the documents selected so far.
        bytes_done: Combined size of the documents finished so far,
            whatever their outcome.
        bytes_downloaded: Combined size of the documents that were really
            downloaded (not skipped or failed); basis for a speed figure.
    """

    expected: int = 0
    ok: int = 0
    skipped: int = 0
    repaired: int = 0
    hash_mismatch: int = 0
    failed: int = 0
    cancelled: bool = False
    zip_path: Optional[Path] = None
    bytes_total: int = 0
    bytes_done: int = 0
    bytes_downloaded: int = 0

    @property
    def has_problems(self) -> bool:
        """Whether any document failed or did not match its hash."""
        return bool(self.failed or self.hash_mismatch)


def normalize_base(url: str) -> str:
    """Returns the API base URL for a Papra address.

    Args:
        url: Address of the instance, with or without a trailing ``/api``.

    Returns:
        The address without a trailing slash, ending in ``/api``.
    """
    base = url.strip().rstrip("/")
    if not base.endswith("/api"):
        base += "/api"
    return base


def install_user_agent(user_agent: str) -> None:
    """Makes every later ``urllib`` request send the given User-Agent.

    Args:
        user_agent: Value of the ``User-Agent`` header.
    """
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", user_agent)]
    urllib.request.install_opener(opener)


def fetch_organizations(base: str, token: str) -> List[Document]:
    """Lists the organizations the token can access.

    Args:
        base: API base URL without a trailing slash.
        token: The API token.

    Returns:
        The organization objects.

    Raises:
        urllib.error.HTTPError: If the API returns an error status.
        urllib.error.URLError: If the server cannot be reached.
        KeyError: If the response has an unexpected shape.
    """
    return get_json(base, token, "/organizations")["organizations"]


def connection_error_message(e: Exception, base: str) -> str:
    """Explains a failure of ``fetch_organizations`` to a human.

    Args:
        e: The exception that was raised.
        base: The API base URL that was used.

    Returns:
        A message, with a hint when the cause is probably a firewall.
    """
    if isinstance(e, urllib.error.HTTPError):
        hint = ""
        if e.code == 403:
            hint = ("\nA 403 means the request was understood but refused: "
                    "either the token lacks the organizations:read / "
                    "documents:read permissions, or a proxy or firewall in "
                    "front of Papra is blocking the script. In the second "
                    "case try another User-Agent, e.g. \"curl/8.4.0\".")
        return f"Could not list organizations: {http_error_detail(e)}{hint}"
    return f"Could not reach the API at {base}: {e}"


def tag_counts(docs: List[Document]) -> Tuple[Dict[str, int], int]:
    """Counts how many documents carry each tag.

    Args:
        docs: Documents as returned by ``list_documents``.

    Returns:
        A tuple ``(counts_by_tag_name, number_of_untagged_documents)``.
    """
    counts: Dict[str, int] = {}
    untagged = 0
    for d in docs:
        if not d.get("tags"):
            untagged += 1
        for t in d.get("tags", []):
            counts[t["name"]] = counts.get(t["name"], 0) + 1
    return counts, untagged


def run_export(base: str, token: str, out: Path, orgs: List[Document],
               options: Optional[ExportOptions] = None,
               log_fn: Callable[[str], None] = log,
               on_progress: Optional[
                   Callable[[int, int, ExportResult], None]] = None,
               cancel: Optional[threading.Event] = None) -> ExportResult:
    """Downloads the selected documents of the given organizations.

    A manifest merged with any existing ``manifest.json`` is written to
    ``out`` even if the run is interrupted or fails.

    Args:
        base: API base URL without a trailing slash.
        token: The API token.
        out: Output directory; created if missing.
        orgs: Organizations to export, as returned by
            ``fetch_organizations``.
        options: What to export and how to save it; defaults to
            everything, flat, with preserved dates.
        log_fn: Receives human-readable progress lines. Called from the
            calling thread only.
        on_progress: Called as ``on_progress(done, total, result)`` once
            after planning an organization and after every document.
            ``total`` counts the documents of the current organization;
            ``result`` is updated in place. Called from the calling thread.
        cancel: If set while running, no further downloads are started;
            downloads already in flight finish. No zip archive is written
            for a cancelled run.

    Returns:
        The totals of the run. ``cancelled`` is True if ``cancel`` was set.

    Raises:
        ValueError: If ``options.layout`` is not one of ``LAYOUTS``.
        urllib.error.HTTPError: If listing documents fails with an error
            status that is not retried.
        urllib.error.URLError: If the server cannot be reached.
        OSError: If the output directory cannot be written.
    """
    options = options or ExportOptions()
    if options.layout not in LAYOUTS:
        raise ValueError(f"unknown layout {options.layout!r}")
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    result = ExportResult()
    manifest: List[ManifestEntry] = []

    retry_ids: Optional[Set[str]] = None
    if options.retry_failed:
        retry_ids = failed_document_ids(manifest_path)
        if not retry_ids:
            log_fn("No failed documents are recorded in manifest.json.")

    try:
        for org in orgs:
            if cancel is not None and cancel.is_set():
                break
            org_name = org.get("name") or org["id"]
            org_dir = out / sanitize(org_name)
            log_fn(f"\n== Organization: {org_name} ==")

            jobs = []  # (doc, dest, is_trash)
            for trash in ([False, True] if options.include_trash
                          else [False]):
                docs, total = list_documents(base, token, org["id"], trash)
                label = "trashed" if trash else "active"
                log_fn(f"Found {len(docs)} {label} documents "
                       f"(server reports {total}).")
                if retry_ids is not None:
                    docs = [d for d in docs if d["id"] in retry_ids]
                docs = select_documents(docs, options)
                if options.has_filters:
                    log_fn(f"  {len(docs)} match: "
                           f"{options.describe_filters()}")
                result.expected += len(docs)
                result.bytes_total += sum(
                    d.get("originalSize") or 0 for d in docs)
                folder = org_dir / "_trash" if trash else org_dir
                paths = plan_filenames(docs, options.id_prefix,
                                       options.layout, options.date_field)
                jobs += [(d, folder / paths[d["id"]], trash) for d in docs]

            done = 0
            if on_progress is not None:
                on_progress(0, len(jobs), result)
            with ThreadPoolExecutor(max_workers=options.workers) as pool:
                futures = {}
                for d, dest, trash in jobs:
                    fut = pool.submit(
                        download, base, token, org["id"], d, dest,
                        options.verify, options.retry_failed,
                        options.preserve_dates)
                    futures[fut] = (d, dest, trash)
                for fut in as_completed(futures):
                    if cancel is not None and cancel.is_set():
                        for pending in futures:
                            pending.cancel()
                    if fut.cancelled():
                        continue
                    d, dest, trash = futures[fut]
                    done += 1
                    try:
                        status = fut.result()
                    except Exception as e:  # keep going on single failures
                        status = "failed"
                        detail = (http_error_detail(e)
                                  if isinstance(e, urllib.error.HTTPError)
                                  else str(e))
                        log_fn(f"[{done}/{len(jobs)}] FAILED {d['id']} "
                               f"({d.get('name')}): {detail}")
                    else:
                        note = {"hash_mismatch": " (HASH MISMATCH)",
                                "repaired": " (replaced existing file)"
                                }.get(status, "")
                        log_fn(f"[{done}/{len(jobs)}] {status}{note}: "
                               f"{dest.relative_to(out)}")
                    setattr(result, status, getattr(result, status) + 1)
                    size = d.get("originalSize") or 0
                    result.bytes_done += size
                    if status in ("ok", "repaired", "hash_mismatch"):
                        result.bytes_downloaded += size
                    manifest.append({
                        "id": d["id"],
                        "organization": org_name,
                        "name": d.get("name"),
                        "originalName": d.get("originalName"),
                        "mimeType": d.get("mimeType"),
                        "size": d.get("originalSize"),
                        "sha256": d.get("originalSha256Hash"),
                        "createdAt": d.get("createdAt"),
                        "documentDate": d.get("documentDate"),
                        "notes": d.get("notes"),
                        "tags": [t["name"] for t in d.get("tags", [])],
                        "customProperties": d.get("customProperties", []),
                        "deleted": trash,
                        "path": str(dest.relative_to(out)),
                        "status": status,
                    })
                    if on_progress is not None:
                        on_progress(done, len(jobs), result)
    finally:
        merged = load_manifest(manifest_path)
        merged.update({m["id"]: m for m in manifest})
        entries = sorted(
            merged.values(), key=lambda m: (m["organization"], m["path"]))
        manifest_path.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8")

    result.cancelled = cancel is not None and cancel.is_set()
    if options.zip_path is not None:
        if result.cancelled:
            log_fn("\nCancelled: no zip archive was created.")
        else:
            try:
                count = build_zip(options.zip_path, out, manifest)
            except OSError as e:
                log_fn(f"\nERROR: could not create the zip archive: {e}")
            else:
                result.zip_path = options.zip_path
                log_fn(f"\nCreated {options.zip_path} with {count} files.")
    return result


def main() -> None:
    """Runs the export from the command line.

    Lists the organizations visible to the token and, for each one, either
    prints tag counts (``--list-tags``) or downloads the selected documents
    through ``run_export``.

    Raises:
        SystemExit: With a message if the API cannot be reached, the token
            is rejected, or no organization matches ``--org``; with status 1
            if any download failed or did not match its recorded hash.
    """
    args = parse_args()
    base = normalize_base(args.url)
    install_user_agent(args.user_agent)

    try:
        orgs = fetch_organizations(base, args.token)
    except (urllib.error.URLError, KeyError) as e:
        sys.exit(connection_error_message(e, base))

    if args.org:
        orgs = [o for o in orgs if args.org in (o["id"], o.get("name"))]
        if not orgs:
            sys.exit(f"No organization matches '{args.org}'.")

    if args.list_tags:
        for org in orgs:
            log(f"\n== Organization: {org.get('name') or org['id']} ==")
            docs, _ = list_documents(base, args.token, org["id"])
            counts, untagged = tag_counts(docs)
            for name, n in sorted(counts.items(),
                                  key=lambda kv: kv[0].lower()):
                log(f"  {name}: {n}")
            log(f"  (untagged): {untagged}")
        return

    out = Path(args.out)
    zip_path = None
    if args.zip is not None:
        zip_path = Path(args.zip) if args.zip else default_zip_path(out)
    options = ExportOptions(
        tags=args.tag, match_all=args.match == "all", since=args.since,
        until=args.until, date_field=args.date_field,
        mime_types=args.type, names=args.name,
        retry_failed=args.retry_failed, include_trash=args.include_trash,
        id_prefix=args.id_prefix, layout=args.layout,
        preserve_dates=args.preserve_dates, verify=args.verify,
        zip_path=zip_path, workers=args.workers)
    result = run_export(base, args.token, out, orgs, options)

    if options.has_filters and result.expected == 0:
        log("\nNo documents matched the filters. "
            "Run with --list-tags to see the exact tag names.")
        return

    log("\n== Summary ==")
    log(f"Expected: {result.expected}")
    log(f"Downloaded: {result.ok}  "
        f"Skipped (already present): {result.skipped}  "
        f"Repaired: {result.repaired}  "
        f"Hash mismatch: {result.hash_mismatch}  "
        f"Failed: {result.failed}")
    if result.zip_path:
        log(f"Zip archive: {result.zip_path}")
    log(f"Manifest: {out / 'manifest.json'}")
    if result.has_problems:
        log("Some documents need attention - rerun with --retry-failed "
            "to try them again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
