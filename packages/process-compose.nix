{
  buildbot-nix,
  buildbot-effects ? null,
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
  # Only the dependencies: buildbot-nix itself runs from the local
  # checkout via PYTHONPATH so code changes apply on restart
  # without a rebuild.
  pythonEnv = python.withPackages (_: buildbot-nix.dependencies);

  # `__GIT_ROOT__` is substituted by the init process so the same
  # store path works in any worktree.
  serviceConfig = writeText "buildbot-nix.json" (
    builtins.toJSON {
      db_url = "postgresql:///buildbot-nix?host=__GIT_ROOT__/.buildbot-dev/pgsock";
      build_systems = [ stdenv.hostPlatform.system ];
      domain = "localhost";
      url = "http://localhost:8010/";
      state_dir = "__GIT_ROOT__/.buildbot-dev/state";
      # The default under /nix/var/nix/gcroots is root-owned; outside
      # it nix-store --add-root registers indirect roots instead.
      gcroots_dir = "__GIT_ROOT__/.buildbot-dev/gcroots";
      allow_unauthenticated_control = true;
      # Pull-based repos need no forge credentials: poll the local
      # checkout itself for convenient hacking.
      pull_based = {
        repositories = {
          buildbot-nix = {
            name = "buildbot-nix";
            default_branch = "__GIT_BRANCH__";
            url = "__GIT_ROOT__";
            poll_interval = 30;
          };
        };
      };
    }
  );

  initScript = writeShellScript "buildbot-dev-init" ''
    set -eu
    git_root=$(git rev-parse --show-toplevel)
    dev="$git_root/.buildbot-dev"
    mkdir -p "$dev/pgsock" "$dev/state"
    if [ ! -d "$dev/pgdata" ]; then
      initdb -D "$dev/pgdata" --auth=trust >/dev/null
    fi
    # Build whatever branch the checkout has checked out.
    git_branch=$(git -C "$git_root" symbolic-ref --short HEAD)
    sed -e "s|__GIT_ROOT__|$git_root|g" \
        -e "s|__GIT_BRANCH__|$git_branch|g" \
        ${serviceConfig} > "$dev/buildbot-nix.json"
  '';

  # process-compose already runs commands through a shell; resolve the
  # state dir from the checkout so the config is location-independent.
  cmd = body: "dev=$(git rev-parse --show-toplevel)/.buildbot-dev; ${body}";

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
            exec.command = cmd ''pg_isready -h "$dev/pgsock" -d buildbot-nix'';
            period_seconds = 1;
            failure_threshold = 30;
          };
          shutdown.signal = 2; # SIGINT: fast shutdown
        };
        createdb = {
          command = cmd ''createdb -h "$dev/pgsock" buildbot-nix 2>/dev/null || true'';
          depends_on.postgres.condition = "process_healthy";
        };
        buildbot-nix = {
          command = cmd ''
            git_root=$(git rev-parse --show-toplevel)
            # No ''${VAR} syntax here: process-compose expands braced
            # variables in commands at config load time.
            export PYTHONPATH="$git_root/buildbot_nix"
            # A delegated scope from the user manager lets the service
            # create memory-capped eval cgroups, like the Delegate=
            # service does in production.
            if command -v systemd-run >/dev/null; then
              run="systemd-run --user --scope -p Delegate=memory --quiet --"
            else
              run=""
            fi
            # -P keeps the cwd off sys.path: the checkout's outer
            # buildbot_nix/ project dir would shadow the package.
            exec $run python -P -m buildbot_nix.main --config "$dev/buildbot-nix.json" --log-format text
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
# Wrapped process-compose preloaded with the buildbot-nix dev stack
# (postgres + buildbot-nix). `process-compose` (no args) starts it;
# subcommands like `down` or `process logs buildbot-nix` pass through.
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
    buildbot-effects
    bubblewrap
  ];
  runtimeEnv = {
    PC_CONFIG_FILES = processComposeConfig;
  };
  # A unix socket instead of the default tcp port 8080 avoids
  # clashes with other process-compose instances on the machine.
  text = ''
    socket=$(git rev-parse --show-toplevel)/.buildbot-dev/process-compose.sock
    mkdir -p "$(dirname "$socket")"
    exec ${lib.getExe process-compose} --use-uds --unix-socket "$socket" "''${@:-up}"
  '';
}
