# Papra Mass Exporter

A bulk document exporter for self-hosted [Papra](https://papra.app) instances. It downloads the original file of every document in one or more organizations, optionally filtered by tag, date, file type or name, verifies each download against the SHA-256 hash recorded by Papra, and writes a `manifest.json` describing everything it exported. I created this app as Papra currently has no native mass export feature. In the future, Papra may add this functionality, upon which this app will be obsolete.

The project ships two entry points:

- **`app/papra_export.py`** — a command-line tool built on the Python standard library only. No third-party packages are required to run it.
- **`app/papra_gui.py`** — a Flet desktop GUI that wraps the exporter with a three-step wizard (Connect → Select → Export), live progress, and an on-screen log. The GUI is purely optional and only exists to provide a more user-friendly interface, and extra convenience over the command-line.

## Features

- Exports every document from one or more Papra organizations.
- Flexible filters that combine with AND: tags (match any or all selected tags), a date range (`--since` / `--until`), MIME type or file extension (`--type`), and a name substring or wildcard pattern (`--name`).
- Hand-pick individual documents from the filtered results in the GUI using a searchable picker.
- Optional inclusion of trashed documents.
- Organizes output with a folder layout: all in one folder, by year, by year and month, or by first tag.
- Sets saved files' modification times to the document dates (opt out with `--no-preserve-dates`).
- Verifies each file against the SHA-256 hash Papra recorded, and can re-hash files already on disk and repair any that do not match (`--verify`).
- Retries just the documents an earlier run recorded as failed or mismatched (`--retry-failed`).
- Optionally bundles a run's documents into a zip archive (`--zip`).
- Safe to re-run: completed files are skipped, interrupted downloads never leave a truncated file under its final name, and the manifest is merged by document ID so partial runs do not erase earlier entries.
- Concurrent downloads with configurable worker count.
- Cross-platform, filesystem-safe filename sanitization (handles Windows reserved names, illegal characters, and duplicate names).
- Manifest captures tags, notes, dates, custom properties, hash, saved path, and per-document status.
- In the GUI: live progress with transferred size, download speed and a time-left estimate, a free-space check for the output drive, and a sound plus window focus when a long export finishes.

## Requirements

- Python 3.14 or later.
- A Papra API token with the `organizations:read` and `documents:read` permissions. A read-only token is recommended.
- For the GUI: [Flet](https://flet.dev) (`flet[all]`, already declared in `pyproject.toml`).

## Installation

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Command line usage

Set the token through the environment or pass it on the command line:

```bash
export PAPRA_URL="https://papra.example.com"
export PAPRA_TOKEN="your_api_token"
uv run python app/papra_export.py --out papra-export
```

Common options:

```bash
# Export a single organization
uv run python app/papra_export.py --org "My Org" --out papra-export

# Export only documents carrying a tag (case-insensitive; name or ID)
uv run python app/papra_export.py --tag invoice --out papra-export

# Require several tags at once, and include trashed documents
uv run python app/papra_export.py --tag invoice --tag 2026 --match all --include-trash

# Filter by date range, file type and name (all filters combine with AND)
uv run python app/papra_export.py --since 2026-01-01 --until 2026-06-30 --type pdf --name "invoice-*"

# Organize the output by year and month, by year, or by first tag
uv run python app/papra_export.py --layout year-month

# Re-hash existing files and repair any that do not match
uv run python app/papra_export.py --verify

# Retry only the documents that failed or mismatched earlier
uv run python app/papra_export.py --retry-failed

# Also write this run's documents into a zip archive
uv run python app/papra_export.py --tag invoice --zip invoices.zip

# List available tags with document counts, without downloading anything
uv run python app/papra_export.py --list-tags
```

Run `--help` for the full list of options (`--date-field`, `--no-preserve-dates`, `--workers`, `--id-prefix`, `--user-agent`, and more).

### Output layout

```
<out>/<organization name>/[<layout folders>/]<document name>
<out>/<organization name>/_trash/...   (only with --include-trash)
<out>/manifest.json
```

With a layout other than `--layout flat` (the default), documents are nested in `year/`, `year/month/` or first-tag folders. Undated documents go into `undated` and untagged ones into `_untagged`.

### Exit status

- `0` if every selected document was exported (or skipped as already present).
- Non-zero if the API could not be reached, no organization matched, or any download failed or did not match its recorded hash. Re-run with `--retry-failed` to retry just those documents.

If a server answers HTTP 403 even though the token is valid, a proxy or firewall may be blocking Python's default `User-Agent`. The exporter sends `papra-export/1.0` by default; override it with `--user-agent`.

## GUI usage

```bash
uv run python app/papra_gui.py
```

The GUI walks through three steps:

1. **Connect** — enter the server URL and API token, then test the connection. The token is never written to disk and must be re-entered each session.
2. **Select** — choose an organization or **All organizations**, pick tags (any/all), apply filters (date range, file type, name), and optionally hand-pick individual documents from the filtered results with a searchable picker. Decide whether to include trash, pick a folder layout and output folder, and toggle options such as preserving file dates, verifying existing files, creating a zip archive, or retrying only previously failed documents. The summary shows the match count and total size, and warns if the output drive may not have enough free space.
3. **Export** — watch live progress and per-status counters (downloaded, skipped, repaired, failed, hash mismatch), along with transferred size, download speed and a time-left estimate. Cancel a running export, retry failed documents, and open the output folder, zip archive, or manifest when finished. A summary banner reports how the run ended, and exports that run for a while play a short sound and bring the window to the front when done.

The server URL, User-Agent, output folder, folder layout, date and verification preferences, and theme are remembered in `~/.papra-exporter.json`. Filter selections and hand-picked documents are not remembered.

The GUI bundles its IBM Plex Sans and Mono fonts under `app/assets/fonts`, so it works fully offline; the only network access is to the Papra instance you connect to.

## Building binaries

The GUI is a Flet app and can be packaged as a standalone desktop binary. With Flet installed, run `flet build` from the directory that holds the Python entry point, passing the target platform:

```bash
cd app
flet build linux     # or macos / windows
```

Flet packages a Python app rooted at a directory (default `.`) and expects a `main.py` entry point, so rename or add a `main.py` that launches `papra_gui.py` if needed. Prebuilt binaries, where provided, are distributed as-is (see the disclaimer below).

### Fast builds with `flet pack`

If you just want a desktop binary quickly, `flet pack` is a lighter-weight alternative to `flet build`. It wraps PyInstaller and turns the script directly into an executable for the current platform, without the fuller Flet build pipeline:

```bash
uv run --extra build flet pack ./app/papra_gui.py --add-data "app/assets:assets"
```

`flet pack` requires PyInstaller, which is declared as the optional `build` extra in `pyproject.toml`, so select it with `--extra build` (otherwise you get `No module named 'PyInstaller'`). The `--add-data` argument bundles the `app/assets` folder (fonts) so the packaged app works offline; on Windows use `;` instead of `:` as the separator. The result is written to `dist/` (on Linux, `dist/papra_gui` plus a `papra_gui.desktop` entry). Because PyInstaller does not cross-compile, the binary only runs on the OS and architecture it was built on.

## Disclaimer

- This codebase was written with the aid of both **Claude Sonnet 5.5** and OpenCode's **Big Pickle**.
- This project has **no affiliation or connection with Papra**. It is an independent, community-made exporter and is not endorsed by or associated with the Papra project.
- Any provided binaries are distributed **as-is, with no warranty of any kind**. The authors accept **no responsibility or liability for any data loss** or other damages arising from the use of this software. Always keep backups, and test on a copy of your data first.

## Contributing

Contributions are welcome. Found a bug or want a feature? Open an issue. See [CONTRIBUTING.md](CONTRIBUTING.md) for reporting guidelines, setup instructions, and how to submit changes.

## License

Released under the MIT License. See [LICENSE](LICENSE).
