# Security Policy

## Reporting a vulnerability

Email **jeevanthandi@googlemail.com** with details and reproduction steps. Please do not open a public issue for security-sensitive reports. You'll get an acknowledgement within a few days.

## Threat model

`analyze` is a read-only static analyzer. Its only subprocesses run coverage.py and your test suite. `mutate` writes to your sources on purpose and restores them. Concretely:

* **No network, no telemetry.** slopguard-python never makes network requests and collects no usage data.
* `analyze` reads `.py` files and never writes to them. Coverage data is written only to a temporary directory the tool owns, and that directory is removed when the run finishes.
* `mutate` writes one mutant at a time into a source file, runs your tests, and then writes the original bytes and timestamps back. It restores the file after every test run, on errors, and on SIGINT, SIGTERM and SIGHUP.
* Before each mutant write, `mutate` checks that the file still holds the bytes it planned the mutants from. That check catches an edit to a file `mutate` has not reached yet, or one saved between two mutant runs. The file keeps that edit, and its remaining mutants are not run. An edit saved while the file holds a mutant is overwritten when `mutate` restores the original. Do not edit files under `--path` during a run.
* `mutate` keeps a backup of the file it is mutating and a journal in `<OS temp dir>/slopguard-mutate/<project hash>/`, never inside your project. If a run is killed, the next run restores the file, but only when the file still holds exactly that mutant. A lock allows one `mutate` run per project. `--dry-run` writes nothing.
* In the default (`auto`) coverage mode, `analyze` spawns only the Python interpreter it runs under. It checks that coverage.py is installed (`python -m coverage --version`). It then runs your project's own test suite under coverage.py in your project root (`python -m coverage run -m pytest` or `-m unittest discover`), followed by coverage.py's report step (`python -m coverage json`). Your tests run as they always do: slopguard-python does not inject code or override your test configuration. Use `--no-coverage` to spawn nothing, or `--coverage-file` to join a `coverage json` report you already produced.
* `mutate` runs the same coverage steps unless you pass `--no-coverage`. It also runs the test suite in your project root without coverage.py: `python -m pytest -x -q -p no:cacheprovider` or `python -m unittest discover -f`, once as a baseline and once per mutant. For pytest, a one-line `python -c` check first tests whether pytest-cov is installed. `mutate` adds `--no-cov` only when it is.
* **Running tests executes your code.** Because gathering coverage means running your test suite, `auto` mode executes whatever your tests execute. This is the same trust boundary as running `pytest` yourself. In untrusted checkouts, prefer `--no-coverage` (complexity-only) or review the suite first.
* `mutate` always runs your test suite, so it always executes whatever your tests execute. Review the suite before you run `mutate` in an untrusted checkout.

## Supply chain

* **Zero runtime dependencies.** slopguard-python is built entirely on the Python standard library (`ast`, `argparse`, `json`). There is no transitive dependency surface to audit. coverage.py lives in *your* project's environment — slopguard only invokes it.
* Releases are tagged from CI and published to PyPI from source.

## Supported versions

slopguard-python is alpha (v0.2.x). Security fixes land on the latest minor release.
