{
  config,
  options,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.buildbot-nix;
  inherit (config.services.buildbot-nix) packages;
  inherit (lib) mkRemovedOptionModule;

  cleanUpRepoName =
    name: builtins.replaceStrings [ "/" ":" "*" ] [ "_slash_" "_colon_" "_star_" ] name;

  webUnixSocket = "/run/buildbot-nix/web.sock";

  hasSSL =
    if cfg.nginx.enable then
      let
        host = config.services.nginx.virtualHosts.${cfg.domain};
      in
      host.forceSSL || host.addSSL || cfg.useHTTPS
    else
      cfg.useHTTPS;

  # Without the managed nginx vhost (or an external TLS proxy implied by
  # useHTTPS), the engine only listens on cfg.port.
  baseUrl = "${if hasSSL then "https" else "http"}://${cfg.domain}${
    lib.optionalString (!cfg.nginx.enable && !cfg.useHTTPS) ":${toString cfg.port}"
  }/";

  localDbUrl = "postgresql://buildbot-nix@/buildbot-nix?host=/run/postgresql";

  engineConfig = (pkgs.formats.json { }).generate "buildbot-nix-config.json" (
    {
      build_systems = cfg.buildSystems;
      domain = cfg.domain;
      url = baseUrl;
      webhook_base_url = cfg.webhookBaseUrl;
      state_dir = "/var/lib/buildbot-nix";
      use_https = cfg.useHTTPS;
      admins = cfg.admins;
      eval_max_memory_size = cfg.evalMaxMemorySize;
      eval_worker_count = cfg.evalWorkerCount;
      build_concurrency = cfg.buildConcurrency;
      gitea =
        if !cfg.gitea.enable then
          null
        else
          {
            instance_url = cfg.gitea.instanceUrl;
            filters = {
              user_allowlist = cfg.gitea.userAllowlist;
              repo_allowlist = cfg.gitea.repoAllowlist;
              topic = cfg.gitea.topic;
            };
            token_file = "gitea-token";
            oauth_id = cfg.gitea.oauthId;
            oauth_secret_file = if cfg.gitea.oauthSecretFile != null then "gitea-oauth-secret" else null;
            ssh_private_key_file = if cfg.gitea.sshPrivateKeyFile != null then "gitea-ssh-key" else null;
            ssh_known_hosts_file = cfg.gitea.sshKnownHostsFile;
          };
      gitlab =
        if !cfg.gitlab.enable then
          null
        else
          {
            instance_url = cfg.gitlab.instanceUrl;
            filters = {
              user_allowlist = cfg.gitlab.userAllowlist;
              repo_allowlist = cfg.gitlab.repoAllowlist;
              topic = cfg.gitlab.topic;
            };
            token_file = "gitlab-token";
            ssh_private_key_file = if cfg.gitlab.sshPrivateKeyFile != null then "gitlab-ssh-key" else null;
            ssh_known_hosts_file = cfg.gitlab.sshKnownHostsFile;
          };
      github =
        if !cfg.github.enable then
          null
        else
          {
            id = cfg.github.appId;
            api_url = cfg.github.apiUrl;
            secret_key_file = "github-app-secret-key";
            webhook_secret_file = "github-webhook-secret";
            filters = {
              user_allowlist = cfg.github.userAllowlist;
              repo_allowlist = cfg.github.repoAllowlist;
              topic = cfg.github.topic;
            };
            oauth_id = cfg.github.oauthId;
            oauth_secret_file = if cfg.github.oauthSecretFile != null then "github-oauth-secret" else null;
          };
      pull_based =
        if cfg.pullBased.repositories == { } then
          null
        else
          {
            repositories = lib.flip lib.mapAttrs cfg.pullBased.repositories (
              name: repo: {
                inherit name;
                default_branch = repo.defaultBranch;
                url = repo.url;
                poll_interval = repo.pollInterval;
                ssh_private_key_file =
                  if repo.sshPrivateKeyFile != null then "pull-based__${cleanUpRepoName name}" else null;
                ssh_known_hosts_file = repo.sshKnownHostsFile;
              }
            );
            poll_spread = cfg.pullBased.pollSpread;
          };
      oidc =
        if !cfg.oidc.enable then
          null
        else
          {
            name = cfg.oidc.name;
            discovery_url = cfg.oidc.discoveryUrl;
            client_id = cfg.oidc.clientId;
            scope = cfg.oidc.scope;
            mapping = cfg.oidc.mapping;
            client_secret_file = "oidc-client-secret";
          };
      outputs_path = cfg.outputsPath;
      post_build_steps = map (step: {
        name = step.name;
        environment = step.environment;
        command = step.command;
        warn_only = step.warnOnly;
      }) cfg.postBuildSteps;
      failed_build_report_limit = cfg.failedBuildReportLimit;
      branches = cfg.branches;
      gcroots_dir = "/nix/var/nix/gcroots/per-user/buildbot-nix";
      effects_per_repo_secrets = lib.mapAttrs' (name: _path: {
        inherit name;
        value = "effects-secret__${cleanUpRepoName name}";
      }) cfg.effects.perRepoSecretFiles;
      effects_extra_sandbox_paths = cfg.effects.extraSandboxPaths;
      show_trace_on_failure = cfg.showTrace;
      cache_failed_builds = cfg.cacheFailedBuilds;
      allow_unauthenticated_control = cfg.allowUnauthenticatedControl;
      build_max_silent_time = cfg.buildMaxSilentTime;
      build_timeout = cfg.buildTimeout;
      http_port = cfg.port;
      http_unix_socket = if cfg.nginx.enable then webUnixSocket else null;
    }
    // (
      if cfg.database.createLocally then
        { db_url = localDbUrl; }
      else if cfg.database.urlFile != null then
        { db_url_file = "db-url"; }
      else
        { db_url = cfg.database.url; }
    )
  );

  # Most master options map 1:1 onto the engine; renames migrate old
  # configurations automatically (with deprecation warnings).
  renameMaster =
    old: new:
    lib.mkRenamedOptionModule
      (
        [
          "services"
          "buildbot-nix"
          "master"
        ]
        ++ old
      )
      (
        [
          "services"
          "buildbot-nix"
        ]
        ++ new
      );

  renamedMaster =
    map (path: renameMaster path path) (
      [
        [ "enable" ]
        [ "domain" ]
        [ "webhookBaseUrl" ]
        [ "useHTTPS" ]
        [ "admins" ]
        [ "buildSystems" ]
        [ "evalMaxMemorySize" ]
        [ "evalWorkerCount" ]
        [ "showTrace" ]
        [ "buildMaxSilentTime" ]
        [ "buildTimeout" ]
        [ "cacheFailedBuilds" ]
        [ "allowUnauthenticatedControl" ]
        [ "outputsPath" ]
        [ "failedBuildReportLimit" ]
        [ "branches" ]
        [ "postBuildSteps" ]
      ]
      ++
        map
          (n: [
            "github"
            n
          ])
          [
            "enable"
            "userAllowlist"
            "repoAllowlist"
            "appId"
            "appSecretKeyFile"
            "webhookSecretFile"
            "oauthId"
            "oauthSecretFile"
            "topic"
          ]
      ++
        map
          (n: [
            "gitea"
            n
          ])
          [
            "enable"
            "userAllowlist"
            "repoAllowlist"
            "tokenFile"
            "oauthId"
            "oauthSecretFile"
            "instanceUrl"
            "topic"
            "sshPrivateKeyFile"
            "sshKnownHostsFile"
          ]
      ++
        map
          (n: [
            "oidc"
            n
          ])
          [
            "name"
            "discoveryUrl"
            "clientId"
            "clientSecretFile"
            "scope"
            "mapping"
          ]
      ++
        map
          (n: [
            "pullBased"
            n
          ])
          [
            "repositories"
            "pollInterval"
            "pollSpread"
            "sshPrivateKeyFile"
            "sshKnownHostsFile"
          ]
      ++
        map
          (n: [
            "effects"
            n
          ])
          [
            "perRepoSecretFiles"
            "extraSandboxPaths"
          ]
      ++
        map
          (n: [
            "cachix"
            n
          ])
          [
            "enable"
            "name"
            "auth"
            "signingKeyFile"
            "authTokenFile"
          ]
      ++
        map
          (n: [
            "niks3"
            n
          ])
          [
            "enable"
            "serverUrl"
            "authTokenFile"
            "package"
          ]
    )
    ++ [
      (renameMaster
        [ "enableNginx" ]
        [
          "nginx"
          "enable"
        ]
      )
    ];

  # Options without an engine equivalent error out with migration hints
  # instead of being silently ignored.
  removedMasterWorker =
    map
      (
        name:
        mkRemovedOptionModule [ "services" "buildbot-nix" "worker" name ] ''
          buildbot-nix no longer uses buildbot workers; builds run inside the
          single engine service. Remove the worker module from your
          configuration.
        ''
      )
      [
        "enable"
        "name"
        "workerPasswordFile"
        "workers"
        "masterUrl"
      ]
    ++ (
      let
        hint = ''
          Enable forges explicitly (github.enable, gitea.enable); every
          enabled forge with oauthId/oauthSecretFile set offers a login,
          OIDC via oidc.enable.
        '';
      in
      [
        (mkRemovedOptionModule [ "services" "buildbot-nix" "authBackend" ] hint)
        (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "authBackend" ] hint)
      ]
    )
    ++ [
      (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "dbUrl" ] ''
        A local PostgreSQL over the unix socket is provisioned by default.
        For a remote database set services.buildbot-nix.database.url or
        database.urlFile and disable database.createLocally.
      '')
      (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "workersFile" ] ''
        Worker passwords are gone together with the buildbot worker protocol.
      '')
      (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "localWorkers" ] ''
        Local workers are gone; the engine builds in-process via the nix daemon.
      '')
      (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "httpBasicAuthPasswordFile" ] ''
        HTTP basic auth was removed together with the oauth2-proxy deployment
        mode. Use the built-in OAuth/OIDC login and per-user API tokens.
      '')
      (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "accessMode" ] ''
        The oauth2-proxy "fullyPrivate" access mode was removed. The built-in
        login hides private repositories from unauthorized users by default.
      '')
      (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "github" "tokenFile" ] ''
        GitHub token authentication was removed; create a GitHub App and set
        services.buildbot-nix.github.appId / appSecretKeyFile.
      '')
      (mkRemovedOptionModule [ "services" "buildbot-nix" "master" "gitea" "webhookSecretFile" ] ''
        Gitea webhook secrets are generated per repository and stored in the
        database; the option is gone.
      '')
    ];
in
{
  imports = [
    ./packages.nix
    ./cachix.nix
    ./niks3.nix
  ]
  ++ renamedMaster
  ++ removedMasterWorker;

  options.services.buildbot-nix = {
    enable = lib.mkEnableOption "the buildbot-nix CI engine";

    domain = lib.mkOption {
      type = lib.types.str;
      description = "Domain under which the web frontend is reachable.";
      example = "ci.example.com";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8010;
      description = "TCP port the engine listens on.";
    };

    webhookBaseUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "URL base for registered webhooks when it differs from the frontend URL.";
      example = "https://ci-webhooks.example.com/";
    };

    useHTTPS = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Force https:// URLs when running behind a reverse proxy other than the
        nginx virtual host managed by this module.
      '';
    };

    admins = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Users allowed to trigger builds and change settings.
        Entries must be provider-qualified, e.g. "github:alice",
        "gitea:bob" or "oidc:<issuer>:carol"; plain usernames never match.
      '';
      example = [ "github:alice" ];
    };

    buildSystems = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ pkgs.stdenv.hostPlatform.system ];
      defaultText = lib.literalExpression "[ pkgs.stdenv.hostPlatform.system ]";
      description = "Systems to build (others come via nix remote builders).";
    };

    buildConcurrency = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      description = "Global cap on concurrent attribute builds. Defaults to the CPU count.";
    };

    evalMaxMemorySize = lib.mkOption {
      type = lib.types.int;
      default = 2048;
      description = "Maximum memory size for nix-eval-jobs (in MiB) per worker.";
    };

    evalWorkerCount = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      description = "Number of nix-eval-jobs worker processes; null uses the core count.";
    };

    showTrace = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Show stack traces on failed evaluations.";
    };

    buildMaxSilentTime = lib.mkOption {
      type = lib.types.int;
      default = 60 * 20;
      description = "Maximum time in seconds a nix build can be silent before being killed.";
    };

    buildTimeout = lib.mkOption {
      type = lib.types.int;
      default = 60 * 60 * 3;
      description = "Overall timeout in seconds for nix builds.";
    };

    cacheFailedBuilds = lib.mkEnableOption "caching failed builds, skipping them until explicitly rebuilt";

    allowUnauthenticatedControl = lib.mkEnableOption ''
      unauthenticated control actions (cancel, restart). Useful behind a VPN
      where network access implies trust
    '';

    failedBuildReportLimit = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 47;
      description = ''
        Maximum number of failed builds reported individually per evaluation
        (3 of the typical 50 commit-status slots stay reserved for the
        eval/build/effects summary statuses).
      '';
    };

    outputsPath = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/var/www/buildbot-nix/nix-outputs/";
      description = ''
        Path where the latest output store paths per attribute are stored as
        text files, exposed via nginx at ''${domain}/nix-outputs.
      '';
    };

    database = {
      createLocally = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Provision a local PostgreSQL database, connected over the unix
          socket with peer authentication.
        '';
      };

      url = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "postgresql://buildbot-nix@db.example.com/buildbot-nix";
        description = "Connection URL for a remote database without secrets.";
      };

      urlFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          File containing the connection URL for a remote database; use this
          when the URL carries a password.
        '';
      };
    };

    github = {
      enable = lib.mkEnableOption "GitHub integration";

      appId = lib.mkOption {
        type = lib.types.int;
        description = "GitHub App ID.";
      };

      apiUrl = lib.mkOption {
        type = lib.types.str;
        default = "https://api.github.com";
        description = "GitHub API base URL (override for GitHub Enterprise).";
      };

      appSecretKeyFile = lib.mkOption {
        type = lib.types.path;
        description = "GitHub App private key file.";
      };

      webhookSecretFile = lib.mkOption {
        type = lib.types.path;
        description = "GitHub webhook secret file.";
      };

      oauthId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "GitHub OAuth client id, used for the login button.";
      };

      oauthSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "GitHub OAuth client secret file.";
      };

      userAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only repositories owned by these users/organizations are built.";
      };

      repoAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only these repositories are built.";
      };

      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Legacy import aid: on the first startup with an empty database,
          repositories carrying this topic are enabled automatically.
          Afterwards the topic is ignored; enable or disable projects in
          the web UI.
        '';
      };
    };

    gitea = {
      enable = lib.mkEnableOption "Gitea integration";

      instanceUrl = lib.mkOption {
        type = lib.types.str;
        description = "Gitea instance URL.";
      };

      tokenFile = lib.mkOption {
        type = lib.types.path;
        description = "Gitea API token file.";
      };

      oauthId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Gitea OAuth client id, used for the login button.";
      };

      oauthSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Gitea OAuth client secret file.";
      };

      userAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only repositories owned by these users/organizations are built.";
      };

      repoAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only these repositories are built.";
      };

      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Legacy import aid: on the first startup with an empty database,
          repositories carrying this topic are enabled automatically.
          Afterwards the topic is ignored; enable or disable projects in
          the web UI.
        '';
      };

      sshPrivateKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "SSH key used to fetch repositories, if non-null.";
      };

      sshKnownHostsFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "known_hosts file matched when fetching over SSH.";
      };
    };

    gitlab = {
      enable = lib.mkEnableOption "GitLab integration";

      instanceUrl = lib.mkOption {
        type = lib.types.str;
        default = "https://gitlab.com";
        description = "GitLab instance URL.";
      };

      tokenFile = lib.mkOption {
        type = lib.types.path;
        description = "GitLab access token file (api scope).";
      };

      userAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only repositories owned by these users/groups are built.";
      };

      repoAllowlist = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        description = "If non-null, only these repositories are built.";
      };

      topic = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = "build-with-buildbot";
        description = ''
          Legacy import aid: on the first startup with an empty database,
          repositories carrying this topic are enabled automatically.
          Afterwards the topic is ignored; enable or disable projects in
          the web UI.
        '';
      };

      sshPrivateKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "SSH key used to fetch repositories, if non-null.";
      };

      sshKnownHostsFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "known_hosts file matched when fetching over SSH.";
      };
    };

    oidc = {
      enable = lib.mkEnableOption "OIDC login";

      name = lib.mkOption {
        type = lib.types.str;
        default = "OIDC Provider";
        description = "User facing name of this provider.";
      };

      discoveryUrl = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "https://id.example.com/.well-known/openid-configuration";
        description = "OIDC discovery endpoint URL.";
      };

      clientId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "OIDC client ID.";
      };

      clientSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "File containing the OIDC client secret.";
      };

      scope = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [
          "openid"
          "email"
          "profile"
        ];
        description = "Requested OIDC scopes.";
      };

      mapping = lib.mkOption {
        description = "How OIDC claims map to user info.";
        default = {
          email = "email";
          username = "preferred_username";
          full_name = "name";
          groups = null;
        };
        type = lib.types.submodule {
          options = {
            email = lib.mkOption {
              type = lib.types.str;
              default = "email";
            };
            username = lib.mkOption {
              type = lib.types.str;
              default = "preferred_username";
            };
            full_name = lib.mkOption {
              type = lib.types.str;
              default = "name";
            };
            groups = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
            };
          };
        };
      };
    };

    pullBased = {
      repositories = lib.mkOption {
        default = { };
        description = "Repositories to poll for changes.";
        type = lib.types.attrsOf (
          lib.types.submodule {
            options = {
              defaultBranch = lib.mkOption {
                type = lib.types.str;
                description = "The repository's default branch.";
              };

              url = lib.mkOption {
                type = lib.types.str;
                description = "The repository's URL, must be fetchable by git.";
              };

              pollInterval = lib.mkOption {
                type = lib.types.addCheck lib.types.int (x: x > 0);
                default = cfg.pullBased.pollInterval;
                description = "How often to poll this repository, in seconds.";
              };

              sshPrivateKeyFile = lib.mkOption {
                type = lib.types.nullOr lib.types.path;
                default = cfg.pullBased.sshPrivateKeyFile;
                description = "SSH key used to fetch this repository.";
              };

              sshKnownHostsFile = lib.mkOption {
                type = lib.types.nullOr lib.types.path;
                default = cfg.pullBased.sshKnownHostsFile;
                description = "known_hosts file matched when fetching over SSH.";
              };
            };
          }
        );
      };

      pollInterval = lib.mkOption {
        type = lib.types.addCheck lib.types.int (x: x > 0);
        default = 60;
        description = "Default poll interval in seconds.";
      };

      pollSpread = lib.mkOption {
        type = lib.types.nullOr lib.types.int;
        default = null;
        description = "Randomly spread polls apart up to this many seconds.";
      };

      sshPrivateKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Default SSH key used to fetch repositories.";
      };

      sshKnownHostsFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Default known_hosts file matched when fetching over SSH.";
      };
    };

    postBuildSteps = lib.mkOption {
      default = [ ];
      description = "Steps to execute after every successful build.";
      type = lib.types.listOf (
        lib.types.submodule {
          options = {
            name = lib.mkOption {
              type = lib.types.str;
              description = "Name of the post-build step, shown in the UI.";
            };

            environment = lib.mkOption {
              type = lib.types.attrsOf lib.types.anything;
              default = { };
              description = ''
                Extra environment variables for this step. Use
                `inputs.buildbot-nix.lib.interpolate "result-%(prop:attr)s"`
                for per-build placeholders. Available properties: attr,
                out_path, drv_path, system, project, branch, revision,
                pr_number, default_branch. `%(secret:NAME)s` reads a
                systemd credential; load it via
                `systemd.services.buildbot-nix.serviceConfig.LoadCredential`.
              '';
            };

            command = lib.mkOption {
              type = lib.types.listOf lib.types.anything;
              description = ''
                Command to execute, passed to execve verbatim (no shell).
              '';
            };

            warnOnly = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Failures only warn instead of failing the build.";
            };
          };
        }
      );
    };

    effects = {
      perRepoSecretFiles = lib.mkOption {
        type = lib.types.attrsOf lib.types.path;
        default = { };
        description = ''
          Per-repository or per-organization JSON secrets files for effects.
          Keys: "github:org/*", "gitea:org/*" or exact "github:owner/repo".
        '';
      };

      extraSandboxPaths = lib.mkOption {
        type = lib.types.listOf lib.types.path;
        default = [ ];
        description = "Extra host paths bind-mounted read-only into the effects sandbox.";
      };
    };

    branches = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            matchGlob = lib.mkOption {
              type = lib.types.str;
              description = "Glob selecting which branches this rule applies to.";
            };

            registerGCRoots = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Register gcroots for matching branches.";
            };

            updateOutputs = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Update the outputs directory for matching branches.";
            };
          };
        }
      );
      default = { };
      description = ''
        Branch rules; matching rules are or-ed together. Default branches
        always behave as if all options were true.
      '';
    };

    nginx = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Manage an nginx virtual host proxying to the engine.";
      };

      enableACME = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Request an ACME certificate and force SSL on the virtual host.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    # A configured but disabled forge is more likely a migration
    # accident than intent.
    warnings =
      lib.optional (!cfg.github.enable && options.services.buildbot-nix.github.appId.isDefined)
        "buildbot-nix: github.* is configured but github.enable is false; GitHub projects will not appear. Set services.buildbot-nix.github.enable = true."
      ++
        lib.optional (!cfg.gitea.enable && options.services.buildbot-nix.gitea.tokenFile.isDefined)
          "buildbot-nix: gitea.* is configured but gitea.enable is false; Gitea projects will not appear. Set services.buildbot-nix.gitea.enable = true."
        ++ lib.optional (!cfg.gitlab.enable && options.services.buildbot-nix.gitlab.tokenFile.isDefined)
          "buildbot-nix: gitlab.* is configured but gitlab.enable is false; GitLab projects will not appear. Set services.buildbot-nix.gitlab.enable = true.";

    assertions = [
      {
        assertion = (cfg.github.oauthId != null) == (cfg.github.oauthSecretFile != null);
        message = "github.oauthId and github.oauthSecretFile must be set together.";
      }
      {
        assertion = (cfg.gitea.oauthId != null) == (cfg.gitea.oauthSecretFile != null);
        message = "gitea.oauthId and gitea.oauthSecretFile must be set together.";
      }
      {
        assertion =
          cfg.oidc.enable
          -> (
            cfg.oidc.discoveryUrl != null && cfg.oidc.clientId != null && cfg.oidc.clientSecretFile != null
          );
        message = "oidc.enable requires oidc.discoveryUrl, oidc.clientId and oidc.clientSecretFile.";
      }
      {
        assertion = cfg.database.createLocally || cfg.database.url != null || cfg.database.urlFile != null;
        message = "Set database.url or database.urlFile when database.createLocally is disabled.";
      }
      {
        assertion =
          cfg.database.createLocally -> (cfg.database.url == null && cfg.database.urlFile == null);
        message = "database.url/database.urlFile are ignored while database.createLocally is enabled; disable it to use a remote database.";
      }
    ];

    users.users.buildbot-nix = {
      isSystemUser = true;
      group = "buildbot-nix";
      home = "/var/lib/buildbot-nix";
    };
    users.groups.buildbot-nix = { };

    nix.settings.extra-allowed-users = [ "buildbot-nix" ];

    systemd.services.buildbot-nix = {
      description = "buildbot-nix CI engine";
      wantedBy = [ "multi-user.target" ];
      after = [
        "network-online.target"
      ]
      ++ lib.optional cfg.database.createLocally "postgresql.target";
      wants = [ "network-online.target" ];
      requires = lib.optional cfg.database.createLocally "postgresql.target";

      path = [
        pkgs.git
        pkgs.openssh
        pkgs.openssl
        pkgs.bash
        pkgs.coreutils
        pkgs.bubblewrap
        pkgs.nix-eval-jobs
        config.nix.package
        # Effects run via the buildbot-effects CLI.
        packages.buildbot-effects
      ];

      environment = {
        # Remote builders need a HOME for ~/.ssh.
        HOME = "/var/lib/buildbot-nix";
      };

      serviceConfig = {
        ExecStart = "${lib.getExe' packages.buildbot-nix "buildbot-nix"} --config ${engineConfig}";
        # Fail activation if the engine never becomes healthy.
        ExecStartPost = pkgs.writeShellScript "buildbot-nix-health" ''
          for _ in $(seq 60); do
            if ${pkgs.curl}/bin/curl -fsS --max-time 5 \
              "http://127.0.0.1:${toString cfg.port}/health" >/dev/null; then
              exit 0
            fi
            sleep 1
          done
          echo "buildbot-nix did not become healthy" >&2
          exit 1
        '';
        User = "buildbot-nix";
        Group = "buildbot-nix";
        StateDirectory = "buildbot-nix";
        # Private repository clones and build logs live here.
        StateDirectoryMode = "0700";
        RuntimeDirectory = "buildbot-nix";
        WorkingDirectory = "/var/lib/buildbot-nix";
        Restart = "on-failure";
        RestartSec = 5;

        LoadCredential =
          lib.optionals cfg.github.enable [
            "github-app-secret-key:${cfg.github.appSecretKeyFile}"
            "github-webhook-secret:${cfg.github.webhookSecretFile}"
          ]
          ++ lib.optional (
            cfg.github.enable && cfg.github.oauthSecretFile != null
          ) "github-oauth-secret:${cfg.github.oauthSecretFile}"
          ++ lib.optional cfg.gitea.enable "gitea-token:${cfg.gitea.tokenFile}"
          ++ lib.optional (
            cfg.gitea.enable && cfg.gitea.oauthSecretFile != null
          ) "gitea-oauth-secret:${cfg.gitea.oauthSecretFile}"
          ++ lib.optional (
            cfg.gitea.enable && cfg.gitea.sshPrivateKeyFile != null
          ) "gitea-ssh-key:${cfg.gitea.sshPrivateKeyFile}"
          ++ lib.optional cfg.gitlab.enable "gitlab-token:${cfg.gitlab.tokenFile}"
          ++ lib.optional (
            cfg.gitlab.enable && cfg.gitlab.sshPrivateKeyFile != null
          ) "gitlab-ssh-key:${cfg.gitlab.sshPrivateKeyFile}"
          ++ lib.optional cfg.oidc.enable "oidc-client-secret:${cfg.oidc.clientSecretFile}"
          ++ lib.optional (
            !cfg.database.createLocally && cfg.database.urlFile != null
          ) "db-url:${cfg.database.urlFile}"
          ++ lib.mapAttrsToList (
            repoName: path: "effects-secret__${cleanUpRepoName repoName}:${path}"
          ) cfg.effects.perRepoSecretFiles
          ++ lib.mapAttrsToList (
            repoName: repo: "pull-based__${cleanUpRepoName repoName}:${repo.sshPrivateKeyFile}"
          ) (lib.filterAttrs (_: repo: repo.sshPrivateKeyFile != null) cfg.pullBased.repositories);

        # Hardening. The eval/effects sandboxes use unprivileged bwrap, so
        # namespace creation must stay allowed and the syscall filter must
        # include the mount family (used inside the new user namespace).
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        # Delegated cgroup subtree: the engine caps each evaluation's
        # process tree with its own memory.max leaf (no polkit needed).
        Delegate = "memory";
        ProtectControlGroups = false;
        ProtectKernelModules = true;
        # ProtectKernelTunables/Logs, ProtectProc and ProtectHostname
        # overmount /proc paths; a fresh proc mount in bwrap's user
        # namespace is then rejected by the kernel, so they stay off.
        ProtectKernelTunables = false;
        ProtectKernelLogs = false;
        ProtectClock = true;
        ProtectProc = "default";
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
        # bwrap --unshare-all needs every type incl. cgroup.
        RestrictNamespaces = "user mnt pid net ipc uts cgroup";
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          # bwrap: new namespaces plus mounts inside them.
          "@mount"
          "unshare"
          "setns"
        ];
        ReadWritePaths = [
          "/nix/var/nix/gcroots/per-user/buildbot-nix"
        ]
        ++ lib.optional (cfg.outputsPath != null) cfg.outputsPath;
      };
    };

    services.postgresql = lib.mkIf cfg.database.createLocally {
      enable = true;
      ensureDatabases = [ "buildbot-nix" ];
      ensureUsers = [
        {
          name = "buildbot-nix";
          ensureDBOwnership = true;
        }
      ];
    };

    services.nginx = lib.mkIf cfg.nginx.enable {
      enable = true;
      virtualHosts.${cfg.domain} = {
        forceSSL = lib.mkIf cfg.nginx.enableACME true;
        enableACME = lib.mkIf cfg.nginx.enableACME true;
        locations = {
          "/" = {
            proxyPass = "http://unix:${webUnixSocket}";
            extraConfig = ''
              proxy_connect_timeout 120s;
              proxy_send_timeout 120s;
              # Long timeout keeps SSE log streams alive.
              proxy_read_timeout 3600s;
              # Buffering would stall SSE.
              proxy_buffering off;
            '';
          };
        }
        // lib.optionalAttrs (cfg.outputsPath != null) {
          "/nix-outputs/" = {
            alias = cfg.outputsPath;
            extraConfig = ''
              charset utf-8;
              autoindex on;
            '';
          };
        };
      };
    };

    users.users.nginx = lib.mkIf cfg.nginx.enable {
      extraGroups = [ "buildbot-nix" ];
    };

    systemd.tmpfiles.rules = [
      "d /nix/var/nix/gcroots/per-user/buildbot-nix 0755 buildbot-nix buildbot-nix - -"
      # Drop gc-roots of builds that have not been refreshed in 90 days.
      "e /nix/var/nix/gcroots/per-user/buildbot-nix/* - - - 90d -"
    ]
    ++ lib.optionals (cfg.outputsPath != null) [
      "d ${cfg.outputsPath} 0755 buildbot-nix buildbot-nix - -"
      # Recursively fix ownership so files created by a previous service user
      # (e.g. after the buildbot -> buildbot-nix migration) stay writable.
      "Z ${cfg.outputsPath} - buildbot-nix buildbot-nix - -"
    ];
  };
}
