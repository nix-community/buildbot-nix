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

# Python tests
nix develop -c pytest
```

## Cleanup

```bash
rm -rf .buildbot-dev
```

## Troubleshooting

- **Port conflict**: Change `PORT = 8012` in `packages/master.cfg.py`
- **Build failures**: Check `systemctl status nix-daemon`
- **Logs**: See `.buildbot-dev/twistd.log`
