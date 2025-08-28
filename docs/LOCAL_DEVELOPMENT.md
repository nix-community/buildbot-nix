# Local Development with buildbot-dev

## Quick Start

```bash
# Run the development server
nix run .#buildbot-dev

# Access web UI at http://localhost:8012
# Login: admin / admin
```

## What It Does

`buildbot-dev` creates a local Buildbot instance in `.buildbot-dev/` with:

- SQLite database
- 4 local workers
- Web interface on port 8012
- Pull-based repository monitoring
- Default configuration for the buildbot-nix repository

## Prerequisites

Ensure GC roots directory exists:

```bash
sudo mkdir -p /nix/var/nix/gcroots/per-user/$(whoami)
sudo chown $(whoami) /nix/var/nix/gcroots/per-user/$(whoami)
```

## Development Workflow

1. **Make changes** to `buildbot_nix/` Python code
2. **Restart** buildbot-dev (Ctrl+C and run again)
3. **Test** through web interface
4. **Check logs** in `.buildbot-dev/twistd.log`

## Testing Your Repository

Edit `packages/master.cfg.py` to add your repository:

```python
repositories={
    "my-project": dict(
        name="my-project",
        url="https://github.com/myorg/my-project",
        default_branch="main",
    )
}
```

## Running Tests

```bash
# Format and lint
nix run .#flake-fmt

# Type checking
nix develop -c mypy buildbot_nix
```

# Debugging NixOS Tests

## Quick Start

Build and run test driver with port forwarding:

```bash
nix build .#checks.x86_64-linux.poller.driver
QEMU_NET_OPTS="hostfwd=tcp:127.0.0.1:8010-:8010" ./result/bin/nixos-test-driver
```

Access Buildbot web UI at http://localhost:8010

For the Gitea integration test:

```bash
nix build .#checks.x86_64-linux.gitea.driver
QEMU_NET_OPTS="hostfwd=tcp:127.0.0.1:8010-:8010,hostfwd=tcp:127.0.0.1:3742-:3742" ./result/bin/nixos-test-driver
```

Access Buildbot at http://localhost:8010 and Gitea at http://localhost:3742

## Using Breakpoints

Add `breakpoint()` in test script to pause execution:

```python
testScript = ''
    buildbot.wait_for_unit("buildbot-master.service")
    breakpoint()  # Drops into Python debugger
    # Continue with 'c', quit with 'q'
''
```

## Cleanup

```bash
rm -rf .buildbot-dev
```

## Troubleshooting

- **Port conflict**: Change `PORT = 8012` in `packages/master.cfg.py`
- **Logs**: See `.buildbot-dev/twistd.log` or console
