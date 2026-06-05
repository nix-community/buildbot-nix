{
  buildbot-nix,
  buildbot-effects ? null,
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
  writeText,
}:
let
  pythonEnv = python.withPackages (_: [ buildbot-nix ]);

  engineConfig = writeText "engine.json" (
    builtins.toJSON {
      db_url = "postgresql:///buildbot-nix?host=__PGHOST__";
      build_systems = [ stdenv.hostPlatform.system ];
      domain = "localhost";
      url = "http://localhost:8010/";
      state_dir = "__STATE_DIR__";
      # Pull-based repositories need no forge credentials: poll the
      # local checkout itself for convenient hacking.
      pull_based = {
        repositories = {
          buildbot-nix = {
            name = "buildbot-nix";
            default_branch = "main";
            url = "__GIT_ROOT__";
            poll_interval = 30;
          };
        };
      };
    }
  );

  processComposeConfig = writeText "process-compose.yaml" (
    builtins.toJSON {
      version = "0.5";
      processes = {
        postgres = {
          command = "postgres -D \"$PC_DEV_DIR/pgdata\" -k \"$PC_DEV_DIR/pgsock\" -c listen_addresses=";
          readiness_probe = {
            exec.command = "pg_isready -h \"$PC_DEV_DIR/pgsock\" -d buildbot-nix";
            period_seconds = 1;
            failure_threshold = 30;
          };
          shutdown.signal = 2; # SIGINT: fast shutdown
        };
        createdb = {
          command = "createdb -h \"$PC_DEV_DIR/pgsock\" buildbot-nix 2>/dev/null || true";
          depends_on.postgres.condition = "process_healthy";
        };
        engine = {
          command = "buildbot-nix --config \"$PC_DEV_DIR/engine.json\" --log-format text";
          depends_on = {
            postgres.condition = "process_healthy";
            createdb.condition = "process_completed_successfully";
          };
          readiness_probe = {
            http_get = {
              host = "127.0.0.1";
              port = 8010;
              path = "/";
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
writeShellApplication {
  name = "buildbot-dev";
  runtimeInputs = [
    pythonEnv
    postgresql
    process-compose
    nix-eval-jobs
    git
    openssh
    nix
    coreutils
  ]
  ++ lib.optional stdenv.isLinux buildbot-effects;
  text = ''
    git_root=$(git rev-parse --show-toplevel)
    dev_dir="$git_root/.buildbot-dev"
    mkdir -p "$dev_dir/pgsock" "$dev_dir/state"

    if [ ! -d "$dev_dir/pgdata" ]; then
      initdb -D "$dev_dir/pgdata" --auth=trust >/dev/null
    fi

    # Regenerated on every start so config changes take effect.
    sed -e "s|__PGHOST__|$dev_dir/pgsock|" \
        -e "s|__STATE_DIR__|$dev_dir/state|" \
        -e "s|__GIT_ROOT__|$git_root|" \
        ${engineConfig} > "$dev_dir/engine.json"

    tui_flag=()
    if [ ! -t 1 ]; then
      # No terminal (CI, logs piped): disable the TUI.
      tui_flag=(--tui=false)
    fi

    echo "buildbot-dev: web UI at http://localhost:8010 once ready" >&2
    PC_DEV_DIR="$dev_dir" exec process-compose up \
      --config ${processComposeConfig} \
      --no-server "''${tui_flag[@]}"
  '';
}
