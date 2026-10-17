# Papra Mass Exporter

A bulk document exporter for self-hosted [Papra](https://papra.app) instances. It downloads the original file of every document in one or more organizations, optionally filtered by tag, verifies each download against the SHA-256 hash recorded by Papra, and writes a `manifest.json` describing everything it exported. I created this app as Papra currently has no native mass export feature. In the future, Papra may add this fucntionality, upon which this app will be obsolete.

The project ships two entry points:

- **`app/papra_export.py`** — a command-line tool built on the Python standard library only. No third-party packages are required to run it.
- **`app/papra_gui.py`** — a Flet desktop GUI that wraps the exporter with a three-step wizard (Connect → Select → Export), live progress, and an on-screen log.

## Features

- Exports every document from one or more Papra organizations.
- Optional tag filtering (match any or all selected tags).
- Optional inclusion of trashed documents.
- Verifies each file against the SHA-256 hash Papra recorded.
- Safe to re-run: completed files are skipped, interrupted downloads never leave a truncated file under its final name, and the manifest is merged by document ID so partial runs do not erase earlier entries.
- Concurrent downloads with configurable worker count.
- Cross-platform, filesystem-safe filename sanitization (handles Windows reserved names, illegal characters, and duplicate names).
- Manifest captures tags, notes, dates, custom properties, hash, saved path, and per-document status.

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

# List available tags with document counts, without downloading anything
uv run python app/papra_export.py --list-tags
```

Run `--help` for the full list of options (`--workers`, `--id-prefix`, `--user-agent`, and more).

### Output layout

```
<out>/<organization name>/<document name>
<out>/<organization name>/_trash/...   (only with --include-trash)
<out>/manifest.json
```

### Exit status

- `0` if every selected document was exported (or skipped as already present).
- Non-zero if the API could not be reached, no organization matched, or any download failed or did not match its recorded hash.

If a server answers HTTP 403 even though the token is valid, a proxy or firewall may be blocking Python's default `User-Agent`. The exporter sends `papra-export/1.0` by default; override it with `--user-agent`.

## GUI usage

```bash
uv run python app/papra_gui.py
```

The GUI walks through three steps:

1. **Connect** — enter the server URL and API token, then test the connection. The token is never written to disk and must be re-entered each session.
2. **Select** — choose an organization, tags (any/all), whether to include trash, and an output folder.
3. **Export** — watch progress, cancel a running export, and open the output folder or manifest when finished.

The server URL, User-Agent, output folder, and theme are remembered in `~/.papra-exporter.json`.

## Building binaries

The GUI is a Flet app and can be packaged as a standalone desktop binary. With Flet installed, run `flet build` from the directory that holds the Python entry point, passing the target platform:

```bash
cd app
flet build linux     # or macos / windows
```

Flet packages a Python app rooted at a directory (default `.`) and expects a `main.py` entry point, so rename or add a `main.py` that launches `papra_gui.py` if needed. Prebuilt binaries, where provided, are distributed as-is (see the disclaimer below).

## Disclaimer

- This codebase was written with the aid of both **Claude Sonnet 5.5** and OpenCode's **Big Pickle**.
- This project has **no affiliation or connection with Papra**. It is an independent, community-made exporter and is not endorsed by or associated with the Papra project.
- Any provided binaries are distributed **as-is, with no warranty of any kind**. The authors accept **no responsibility or liability for any data loss** or other damages arising from the use of this software. Always keep backups, and test on a copy of your data first.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, development guidelines, and how to submit changes.

## License

Released under the MIT License. See [LICENSE](LICENSE).
