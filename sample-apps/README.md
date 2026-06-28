# sample-apps

Reference fixtures used as slopguard-python's CI regression baseline. Drift in
their reported shape signals an analyzer bug, not a code change in the fixture.

## todolist

A tiny, fully-tested todo-list package (low complexity, ~100% coverage). The
baseline assertion expects:

```
methods: 12   crappy: 0   types: 2   coverage: 100%
```

It has its own `pyproject.toml`, so `**/sample-apps/**` is excluded from scans
of the parent repo. Analyze it on demand:

```bash
slopguard-python analyze \
  --path sample-apps/todolist/todolist \
  --project-dir sample-apps/todolist --json --quiet \
  | python -c 'import sys,json; r=json.load(sys.stdin); print(r["summary"])'
```
