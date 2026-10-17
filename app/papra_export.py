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

Listing the available tags with document counts, without downloading:

  python papra_export.py --url ... --list-tags

Output layout:

  <out>/<organization name>/<document name>
  <out>/<organization name>/_trash/...   (only with --include-trash)
  <out>/manifest.json

Exit status:
  0 if every selected document was exported (or skipped as already present).
  Non-zero if the API could not be reached, no organization matched, or any
  download failed or did not match its recorded hash.
"""

import argparse
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

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
# Names Windows refuses to use as a file name, with or without extension.
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
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
                wait = 2**attempt
            err = e
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
        ) as e:
            wait = 2**attempt
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


def make_request(
    base: str, token: str, path: str, params: Optional[Dict[str, Any]] = None
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
    return urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})


def get_json(
    base: str, token: str, path: str, params: Optional[Dict[str, Any]] = None
) -> Any:
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


def list_documents(
    base: str, token: str, org_id: str, trash: bool = False
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
    path = f"/organizations/{org_id}/documents" + ("/deleted" if trash else "")
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


def filter_by_tags(
    docs: List[Document], wanted: Iterable[str], match_all: bool
) -> List[Document]:
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
        return {
            str(tag.get("id", "")).lower(),
            str(tag.get("name", "")).lower(),
            str(tag.get("normalizedName", "")).lower(),
        }

    kept = []
    for d in docs:
        doc_keys = [keys(t) for t in d.get("tags", [])]
        # For each wanted value, does any of the document's tags match it?
        hits = [any(w in k for k in doc_keys) for w in wanted_set]
        if all(hits) if match_all else any(hits):
            kept.append(d)
    return kept


def load_manifest(path: Path) -> Dict[str, ManifestEntry]:
    """Loads an existing manifest so partial runs can merge into it.

    Args:
        path: Location of ``manifest.json``.

    Returns:
        Existing manifest entries keyed by document ID. The result is empty
        if the file is missing, unreadable or malformed.
    """
    try:
        return {m["id"]: m for m in json.loads(path.read_text(encoding="utf-8"))}
    except OSError, ValueError, KeyError, TypeError:
        return {}


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


def plan_filenames(docs: List[Document], id_prefix: bool) -> Dict[str, str]:
    """Chooses a unique file name for every document.

    The sanitized document name is used. A missing extension is guessed
    from the MIME type. If two documents would share a name (compared
    case-insensitively), the later one gets the last six characters of its
    ID appended before the extension.

    Args:
        docs: The documents to name.
        id_prefix: If True, prefix every name with the document ID, which
            makes names unique by construction.

    Returns:
        A mapping of document ID to file name.
    """
    used = set()
    names: Dict[str, str] = {}
    for d in docs:
        name = d.get("name") or d.get("originalName") or d["id"]
        name = sanitize(name)
        if not os.path.splitext(name)[1] and d.get("mimeType"):
            name += mimetypes.guess_extension(d["mimeType"]) or ""
        if id_prefix:
            name = f"{d['id']}_{name}"
        if name.lower() in used:  # duplicate name: add short id suffix
            stem, ext = os.path.splitext(name)
            name = f"{stem}_{d['id'][-6:]}{ext}"
        used.add(name.lower())
        names[d["id"]] = name
    return names


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def download(base: str, token: str, org_id: str, doc: Document, dest: Path) -> str:
    """Downloads one document file and verifies it.

    The file is streamed to ``<dest>.part`` while its SHA-256 is computed,
    then atomically renamed to ``dest``. The partial file is removed if
    anything goes wrong. If ``dest`` already exists with the expected size
    the download is skipped.

    Args:
        base: API base URL without a trailing slash.
        token: The API token.
        org_id: ID of the organization owning the document.
        doc: The document's metadata, as returned by ``list_documents``.
        dest: Where to save the file.

    Returns:
        ``"skipped"`` if a complete file was already present, ``"ok"`` if
        the download matched the recorded hash (or no hash was recorded),
        or ``"hash_mismatch"`` if the file was saved but its SHA-256 did
        not match the hash recorded by Papra.

    Raises:
        urllib.error.HTTPError: If the API returns an error status that is
            not retried, or retries are exhausted.
        urllib.error.URLError: If the server cannot be reached.
        OSError: If the file cannot be written.
    """
    expected_size = doc.get("originalSize")
    expected_hash = doc.get("originalSha256Hash")

    if (
        dest.exists()
        and expected_size is not None
        and dest.stat().st_size == expected_size
    ):
        return "skipped"

    req = make_request(
        base, token, f"/organizations/{org_id}/documents/{doc['id']}/file"
    )
    tmp = dest.with_name(dest.name + ".part")

    def go() -> str:
        """Performs one streamed download attempt; returns its SHA-256."""
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(tmp, "wb") as f:
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

    if expected_hash and digest != expected_hash:
        return "hash_mismatch"
    return "ok"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


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
    p = argparse.ArgumentParser(description="Bulk-export documents from Papra.")
    p.add_argument(
        "--url",
        default=os.environ.get("PAPRA_URL"),
        help="Papra base URL (or env PAPRA_URL), e.g. https://papra.example.com",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("PAPRA_TOKEN"),
        help="API token (or env PAPRA_TOKEN)",
    )
    p.add_argument("--out", default="papra-export", help="Output directory")
    p.add_argument("--org", help="Only export this organization (ID or name)")
    p.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help="Only export documents with this tag (name or ID). "
        "Repeat for multiple tags",
    )
    p.add_argument(
        "--match",
        choices=["any", "all"],
        default="any",
        help="With several --tag values: documents having ANY (default) or ALL of them",
    )
    p.add_argument(
        "--list-tags",
        action="store_true",
        help="Print tags with document counts and exit (downloads nothing)",
    )
    p.add_argument(
        "--include-trash", action="store_true", help="Also export trashed documents"
    )
    p.add_argument(
        "--id-prefix",
        action="store_true",
        help="Prefix filenames with the document ID (guarantees uniqueness)",
    )
    p.add_argument(
        "--workers", type=int, default=4, help="Concurrent downloads (default 4)"
    )
    p.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent header to send (default "
        f"{DEFAULT_USER_AGENT!r}). Some proxies and "
        "firewalls block Python's default one",
    )
    args = p.parse_args()
    if not args.url or not args.token:
        p.error("--url and --token are required (or set PAPRA_URL / PAPRA_TOKEN)")
    return args


@dataclass
class ExportResult:
    """Outcome of ``run_export``.

    Attributes:
        expected: Number of documents selected for export.
        ok: Documents downloaded and verified.
        skipped: Documents that were already present on disk.
        hash_mismatch: Documents saved whose SHA-256 differed from the hash
            recorded by Papra.
        failed: Documents that could not be downloaded.
        cancelled: True if the run was stopped through the cancel event.
    """

    expected: int = 0
    ok: int = 0
    skipped: int = 0
    hash_mismatch: int = 0
    failed: int = 0
    cancelled: bool = False

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
            hint = (
                "\nA 403 means the request was understood but refused: "
                "either the token lacks the organizations:read / "
                "documents:read permissions, or a proxy or firewall in "
                "front of Papra is blocking the script. In the second "
                'case try another User-Agent, e.g. "curl/8.4.0".'
            )
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


def run_export(
    base: str,
    token: str,
    out: Path,
    orgs: List[Document],
    tags: Iterable[str] = (),
    match_all: bool = False,
    include_trash: bool = False,
    id_prefix: bool = False,
    workers: int = 4,
    log_fn: Callable[[str], None] = log,
    on_progress: Optional[Callable[[int, int, ExportResult], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> ExportResult:
    """Downloads the selected documents of the given organizations.

    A manifest merged with any existing ``manifest.json`` is written to
    ``out`` even if the run is interrupted or fails.

    Args:
        base: API base URL without a trailing slash.
        token: The API token.
        out: Output directory; created if missing.
        orgs: Organizations to export, as returned by
            ``fetch_organizations``.
        tags: Only export documents with these tags (names or IDs). Empty
            means all documents.
        match_all: With several tags, require all of them instead of any.
        include_trash: Also export trashed documents.
        id_prefix: Prefix file names with the document ID.
        workers: Number of concurrent downloads.
        log_fn: Receives human-readable progress lines. Called from the
            calling thread only.
        on_progress: Called as ``on_progress(done, total, result)`` once
            after planning an organization and after every document.
            ``total`` counts the documents of the current organization;
            ``result`` is updated in place. Called from the calling thread.
        cancel: If set while running, no further downloads are started;
            downloads already in flight finish.

    Returns:
        The totals of the run. ``cancelled`` is True if ``cancel`` was set.

    Raises:
        urllib.error.HTTPError: If listing documents fails with an error
            status that is not retried.
        urllib.error.URLError: If the server cannot be reached.
        OSError: If the output directory cannot be written.
    """
    tags = list(tags)
    out.mkdir(parents=True, exist_ok=True)
    result = ExportResult()
    manifest: List[ManifestEntry] = []

    try:
        for org in orgs:
            if cancel is not None and cancel.is_set():
                break
            org_name = org.get("name") or org["id"]
            org_dir = out / sanitize(org_name)
            log_fn(f"\n== Organization: {org_name} ==")

            jobs = []  # (doc, dest, is_trash)
            for trash in [False, True] if include_trash else [False]:
                docs, total = list_documents(base, token, org["id"], trash)
                label = "trashed" if trash else "active"
                log_fn(f"Found {len(docs)} {label} documents (server reports {total}).")
                if tags:
                    docs = filter_by_tags(docs, tags, match_all)
                    joiner = " ALL " if match_all else " ANY "
                    log_fn(f"  {len(docs)} match tag filter: {joiner.join(tags)}")
                result.expected += len(docs)
                folder = org_dir / "_trash" if trash else org_dir
                folder.mkdir(parents=True, exist_ok=True)
                names = plan_filenames(docs, id_prefix)
                jobs += [(d, folder / names[d["id"]], trash) for d in docs]

            done = 0
            if on_progress is not None:
                on_progress(0, len(jobs), result)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {}
                for d, dest, trash in jobs:
                    fut = pool.submit(download, base, token, org["id"], d, dest)
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
                        detail = (
                            http_error_detail(e)
                            if isinstance(e, urllib.error.HTTPError)
                            else str(e)
                        )
                        log_fn(
                            f"[{done}/{len(jobs)}] FAILED {d['id']} "
                            f"({d.get('name')}): {detail}"
                        )
                    else:
                        note = " (HASH MISMATCH)" if status == "hash_mismatch" else ""
                        log_fn(f"[{done}/{len(jobs)}] {status}{note}: {dest.name}")
                    setattr(result, status, getattr(result, status) + 1)
                    manifest.append(
                        {
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
                        }
                    )
                    if on_progress is not None:
                        on_progress(done, len(jobs), result)
    finally:
        manifest_path = out / "manifest.json"
        merged = load_manifest(manifest_path)
        merged.update({m["id"]: m for m in manifest})
        entries = sorted(merged.values(), key=lambda m: (m["organization"], m["path"]))
        manifest_path.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    result.cancelled = cancel is not None and cancel.is_set()
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
            for name, n in sorted(counts.items(), key=lambda kv: kv[0].lower()):
                log(f"  {name}: {n}")
            log(f"  (untagged): {untagged}")
        return

    result = run_export(
        base,
        args.token,
        Path(args.out),
        orgs,
        tags=args.tag,
        match_all=args.match == "all",
        include_trash=args.include_trash,
        id_prefix=args.id_prefix,
        workers=args.workers,
    )

    if args.tag and result.expected == 0:
        log("\nNo documents matched. Run with --list-tags to see the exact tag names.")
        return

    log("\n== Summary ==")
    log(f"Expected: {result.expected}")
    log(
        f"Downloaded: {result.ok}  "
        f"Skipped (already present): {result.skipped}  "
        f"Hash mismatch: {result.hash_mismatch}  "
        f"Failed: {result.failed}"
    )
    log(f"Manifest: {Path(args.out) / 'manifest.json'}")
    if result.has_problems:
        log(
            "Some documents need attention - rerun the script to retry "
            "failures (finished files are skipped)."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
