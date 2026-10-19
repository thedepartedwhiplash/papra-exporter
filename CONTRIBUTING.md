# Contributing

Thanks for your interest in improving Papra Mass Exporter. This project is an independent, community-made exporter for self-hosted Papra instances and has no affiliation with Papra.

## Getting started

1. Fork and clone the repository.
2. Install the dependencies with [uv](https://docs.astral.sh/uv/):

   ```bash
   uv sync
   ```

3. Run the app to confirm your setup:

   ```bash
   uv run python app/papra_export.py --help
   uv run python app/papra_gui.py
   ```

## Project layout

- `app/papra_export.py` — command-line exporter (Python standard library only).
- `app/papra_gui.py` — Flet desktop GUI wrapping the exporter.

## Development guidelines

- Keep `app/papra_export.py` free of third-party dependencies; it should run on the standard library alone.
- Follow the existing code style: type hints, docstrings, and no inline comments unless they clarify something non-obvious.
- Verify your changes compile before submitting:

  ```bash
  python -m py_compile app/papra_export.py app/papra_gui.py
  ```

- Test against a self-hosted Papra instance where possible, and never commit API tokens or other secrets.

## Reporting issues

Bug reports and feature requests are welcome through the repository's issue tracker.

- **Bug reports:** describe what you did, what you expected, and what happened instead. Include your OS, the Python version, whether you used the CLI or the GUI, and any relevant error output.
- **Feature requests:** explain the use case and the problem you're trying to solve, not just the solution you have in mind.

Please search existing issues before opening a new one, and never include API tokens or other secrets.

## Submitting changes

1. Create a branch with a descriptive name.
2. Make your changes and confirm the scripts still compile and run with no errors.
3. Open a pull request describing what changed and why.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
