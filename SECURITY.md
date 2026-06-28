# Security Policy

## Reporting a vulnerability

Email **jeevanthandi@googlemail.com** with details and reproduction steps. Please do not open a public issue for security-sensitive reports. You'll get an acknowledgement within a few days.

## Threat model

slopguard-python is a read-only static analyzer with one controlled subprocess. Concretely:

* **No network, no telemetry.** slopguard-python never makes network requests and collects no usage data.
* **No source mutation.** It reads `.py` files and never writes to them. Coverage data is written only to a temporary directory the tool owns, and that directory is removed when the run finishes.
* **One subprocess.** In the default (`auto`) coverage mode the only process slopguard-python spawns is your project's own test suite under coverage.py (`python -m coverage run -m pytest` / `-m unittest discover`), run in your project root. Your tests run as they always do — slopguard-python does not inject code or override your test configuration. Use `--no-coverage` to spawn nothing, or `--coverage-file` to join a `coverage json` report you already produced.
* **Running tests executes your code.** Because gathering coverage means running your test suite, `auto` mode executes whatever your tests execute. This is the same trust boundary as running `pytest` yourself. In untrusted checkouts, prefer `--no-coverage` (complexity-only) or review the suite first.

## Supply chain

* **Zero runtime dependencies.** slopguard-python is built entirely on the Python standard library (`ast`, `argparse`, `json`). There is no transitive dependency surface to audit. coverage.py lives in *your* project's environment — slopguard only invokes it.
* Releases are tagged from CI and published to PyPI from source.

## Supported versions

slopguard-python is alpha (v0.1.x). Security fixes land on the latest minor release.
