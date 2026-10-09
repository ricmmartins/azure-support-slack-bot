# Contributing to Azure Service Health for Slack

Thank you for your interest in contributing! Please open an issue or pull request for any improvements, bug fixes, or new features.

## How to Contribute

- Fork the repository and create your branch from `main`.
- Run `pytest -q`, `flake8 .`, and `pwsh -NoProfile -File test/test-hooks.ps1`.
  Hook tests mock Azure and Graph; never provision resources just to run tests.
- Add new tests for your changes if applicable.
- Open a pull request with a clear description of your changes.

## Code Style

- Follow PEP8 for Python code.
- Use clear, descriptive commit messages.
- **Format your code with [autopep8](https://github.com/hhatto/autopep8) before submitting:**
  - Install with `pip install autopep8`
  - Run `autopep8 --in-place <changed-file.py>` on changed files; avoid recursive
    formatting of virtual environments or unrelated files.


## Reporting Issues

Please use the GitHub Issues tab to report bugs or request features.
