{
  nixbot,
  nixbot-effects ? null,
  bubblewrap,
  coreutils,
  git,
  lib,
  nix,
  nix-eval-jobs,
  openssh,
  postgresql,
  process-compose,
  python,
  stdenv,
  writeShellApplication,
  writeShellScript,
  writeText,
}:
let
  # Only the dependencies: nixbot itself runs from the local
  # checkout via PYTHONPATH so code changes apply on restart
  # without a rebuild.
  pythonEnv = python.withPackages (_: nixbot.dependencies);

  # `__GIT_ROOT__` is substituted by the init process so the same
  # store path works in any worktree.
  serviceConfig = writeText "nixbot.json" (
    builtins.toJSON {
      db_url = "postgresql:///nixbot?host=__GIT_ROOT__/.nixbot-dev/pgsock";
      build_systems = [ stdenv.hostPlatform.system ];
      domain = "localhost";
      url = "http://localhost:8010/";
      state_dir = "__GIT_ROOT__/.nixbot-dev/state";
      # The default under /nix/var/nix/gcroots is root-owned; outside
      # it nix-store --add-root registers indirect roots instead.
      gcroots_dir = "__GIT_ROOT__/.nixbot-dev/gcroots";
      allow_unauthenticated_control = true;
      # Pull-based repos need no forge credentials: poll the local
      # checkout itself for convenient hacking.
      pull_based = {
        repositories = {
          nixbot = {
            name = "nixbot";
            default_branch = "__GIT_BRANCH__";
            url = "__GIT_ROOT__";
            poll_interval = 30;
          };
        };
      };
    }
  );

  initScript = writeShellScript "nixbot-dev-init" ''
    set -eu
    git_root=$(git rev-parse --show-toplevel)
    dev="$git_root/.nixbot-dev"
    mkdir -p "$dev/pgsock" "$dev/state"
    if [ ! -d "$dev/pgdata" ]; then
      initdb -D "$dev/pgdata" --auth=trust >/dev/null
    fi
    # Build whatever branch the checkout has checked out.
    git_branch=$(git -C "$git_root" symbolic-ref --short HEAD)
    sed -e "s|__GIT_ROOT__|$git_root|g" \
        -e "s|__GIT_BRANCH__|$git_branch|g" \
        ${serviceConfig} > "$dev/nixbot.json"
  '';

  # process-compose already runs commands through a shell; resolve the
  # state dir from the checkout so the config is location-independent.
  cmd = body: "dev=$(git rev-parse --show-toplevel)/.nixbot-dev; ${body}";

  processComposeConfig = writeText "process-compose.yaml" (
    builtins.toJSON {
      version = "0.5";
      # The TUI requires /dev/tty, which redirected stdout (CI, nohup)
      # does not have.
      is_tui_disabled = true;
      processes = {
        init.command = "${initScript}";
        postgres = {
          command = cmd ''exec postgres -D "$dev/pgdata" -k "$dev/pgsock" -c listen_addresses='';
          depends_on.init.condition = "process_completed_successfully";
          readiness_probe = {
            exec.command = cmd ''pg_isready -h "$dev/pgsock" -d nixbot'';
            period_seconds = 1;
            failure_threshold = 30;
          };
          shutdown.signal = 2; # SIGINT: fast shutdown
        };
        createdb = {
          command = cmd ''createdb -h "$dev/pgsock" nixbot 2>/dev/null || true'';
          depends_on.postgres.condition = "process_healthy";
        };
        nixbot = {
          command = cmd ''
            git_root=$(git rev-parse --show-toplevel)
            # No ''${VAR} syntax here: process-compose expands braced
            # variables in commands at config load time.
            export PYTHONPATH="$git_root/nixbot"
            # A delegated scope from the user manager lets the service
            # create memory-capped eval cgroups, like the Delegate=
            # service does in production.
            if command -v systemd-run >/dev/null; then
              run="systemd-run --user --scope -p Delegate=memory --quiet --"
            else
              run=""
            fi
            # -P keeps the cwd off sys.path: the checkout's outer
            # nixbot/ project dir would shadow the package.
            exec $run python -P -m nixbot.main --config "$dev/nixbot.json" --log-format text
          '';
          depends_on = {
            postgres.condition = "process_healthy";
            createdb.condition = "process_completed_successfully";
          };
          readiness_probe = {
            http_get = {
              host = "127.0.0.1";
              port = 8010;
              path = "/health";
            };
            initial_delay_seconds = 2;
            period_seconds = 2;
            failure_threshold = 30;
          };
          availability.restart = "on_failure";
        };
      };
    }
  );
in
# Wrapped process-compose preloaded with the nixbot dev stack
# (postgres + nixbot). `process-compose` (no args) starts it;
# subcommands like `down` or `process logs nixbot` pass through.
writeShellApplication {
  name = "process-compose";
  runtimeInputs = [
    pythonEnv
    postgresql
    nix-eval-jobs
    git
    openssh
    nix
    coreutils
  ]
  # The service wraps nix-eval-jobs in a bwrap sandbox on Linux.
  ++ lib.optionals stdenv.isLinux [
    nixbot-effects
    bubblewrap
  ];
  runtimeEnv = {
    PC_CONFIG_FILES = processComposeConfig;
  };
  # A unix socket instead of the default tcp port 8080 avoids
  # clashes with other process-compose instances on the machine.
  text = ''
    socket=$(git rev-parse --show-toplevel)/.nixbot-dev/process-compose.sock
    mkdir -p "$(dirname "$socket")"
    exec ${lib.getExe process-compose} --use-uds --unix-socket "$socket" "''${@:-up}"
  '';
}
