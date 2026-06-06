# Local Development

## Running the test suite

```bash
nix develop
cd buildbot_nix
python -m pytest buildbot_nix/tests -q
```

The tests cover the full pipeline (webhook parsing, evaluation, scheduling,
building, status reporting, web frontend) against an ephemeral PostgreSQL
instance and real `nix`/`git` where available.

## Running the engine locally

The engine is a single process configured by a JSON file:

```bash
# Start an ephemeral PostgreSQL
initdb -D /tmp/bb-pg
pg_ctl -D /tmp/bb-pg -o "-k /tmp/bb-pg -c listen_addresses=" start
createdb -h /tmp/bb-pg buildbot-nix

cat > /tmp/engine.json <<EOF
{
  "db_url": "postgresql://$(whoami)@/buildbot-nix?host=/tmp/bb-pg",
  "build_systems": ["x86_64-linux"],
  "domain": "localhost",
  "url": "http://localhost:8010/",
  "state_dir": "/tmp/buildbot-nix-state",
  "pull_based": {
    "repositories": {
      "my-project": {
        "name": "my-project",
        "default_branch": "main",
        "url": "https://github.com/example/my-project"
      }
    }
  }
}
EOF

python -m buildbot_nix.main --config /tmp/engine.json --log-format text
```

Access the web UI at http://localhost:8010. Pull-based repositories need no
forge credentials, which makes them convenient for local hacking; GitHub/Gitea
configuration works the same way as in the NixOS module, with secret paths
pointing at plain local files.

## VM integration test

The end-to-end NixOS test (fake GitHub + real Gitea) lives in
`checks/engine.nix`:

```bash
nix build .#checks.x86_64-linux.engine -L
```

For interactive debugging:

```bash
nix build .#checks.x86_64-linux.engine.driverInteractive
./result/bin/nixos-test-driver
```

Add `breakpoint()` in the test script to pause execution.

## Code quality

```bash
nix develop -c flake-fmt   # treefmt: ruff format, ruff check, mypy, nixfmt
```
