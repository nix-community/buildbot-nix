{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.buildbot-nix.master;
  inherit (config.services.buildbot-nix) packages;
  inherit (lib) mkRemovedOptionModule mkRenamedOptionModule;

  interpolateType = lib.mkOptionType {
    name = "interpolate";

    description = ''
      A type representing a Buildbot interpolation string, supports interpolations like `result-%(prop:attr)s`.
    '';

    check = x: x ? "_type" && x._type == "interpolate" && x ? "value";
  };

  cleanUpRepoName =
    name:
    builtins.replaceStrings
      [
        "/"
        ":"
      ]
      [
        "_slash_"
        "_colon_"
      ]
      name;

  backendPort =
    if (cfg.accessMode ? "fullyPrivate") then
      cfg.accessMode.fullyPrivate.port
    else
      config.services.buildbot-master.port;
in
{
  imports = [
    ./packages.nix
    ./cachix.nix
    (mkRenamedOptionModule
      [
        "services"
        "buildbot-nix"
        "master"
        "github"
        "admins"
      ]
      [
        "services"
        "buildbot-nix"
        "master"
        "admins"
      ]
    )
    (mkRenamedOptionModule
      [
        "services"
        "buildbot-nix"
        "master"
        "github"
        "tokenFile"
      ]
      [
        "services"
        "buildbot-nix"
        "master"
        "github"
        "authType"
        "legacy"
        "tokenFile"
      ]
    )
    (mkRemovedOptionModule
      [
        "services"
        "buildbot-nix"
        "master"
        "github"
        "user"
      ]
      ''
        Setting a user is not required.
      ''
    )
    (mkRemovedOptionModule
      [
        "services"
        "buildbot-nix"
        "master"
        "buildbotNixpkgs"
      ]
      ''
        Set packages directly in services.buildbot-nix.packages instead.
      ''
    )
  ];

  options = {
    services.buildbot-nix.master = {
      enable = lib.mkEnableOption "buildbot-master";
      dbUrl = lib.mkOption {
        type = lib.types.str;
        default = "postgresql://@/buildbot";
        description = "Postgresql database url";
      };
      authBackend = lib.mkOption {
        type = lib.types.enum [
          "github"
          "gitea"
          "httpbasicauth"
          "none"
        ];
        default = "github";
        description = ''
          Which OAuth2 backend to use.
        '';
      };

      httpBasicAuthPasswordFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          Path to file containing the password used in HTTP basic authentication.
        '';
      };

      postBuildSteps = lib.mkOption {
        default = [ ];
        description = ''
          A list of steps to execute after every successful build.
        '';
        type = lib.types.listOf (
          lib.types.submodule {
            options = {
              name = lib.mkOption {
                type = lib.types.str;
                description = ''
                  The name of the build step, will show up in Buildbot's UI.
                '';
              };

              environment = lib.mkOption {
                type =
                  with lib.types;
                  attrsOf (oneOf [
                    interpolateType
                    str
                  ]);
                description = ''
                  Extra environment variables to add to the environment of this build step.
                  The base environment is the environment of the `buildbot-worker` service.

                  To access the properties of a build, use the `interpolate` function defined in
                  `inputs.buildbot-nix.lib.interpolate` like so `(interpolate "result-%(prop:attr)s")`.
                '';
                default = { };
              };

              command = lib.mkOption {
                type =
                  with lib.types;
                  oneOf [
                    str
                    (listOf (oneOf [
                      str
                      interpolateType
                    ]))
                  ];
                description = ''
                  The command to execute as part of the build step. Either a single string or
                  a list of strings. Be careful that neither variant is interpreted by a shell,
                  but is passed to `execve` verbatim. If you desire a shell, you must use
                  `writeShellScript` or similar functions.

                  To access the properties of a build, use the `interpolate` function defined in
                  `inputs.buildbot-nix.lib.interpolate` like so `(interpolate "result-%(prop:attr)s")`.
                '';
              };
            };
          }
        );

        example = lib.literalExpression ''
          [
            name = "upload-to-s3";
            environment = {
              S3_TOKEN = "%(secret:s3-token)";
              S3_BUCKET = "bucket";
            };
            command = [ "nix" "copy" "%result%" ];
          ]
        '';
      };

      accessMode = lib.mkOption {
        default = {
          public = { };
        };
        type = lib.types.attrTag {
          public = lib.mkOption {
            type = lib.types.submodule { };
            description = ''
              Default public mode, will allow read only access to anonymous users. Authentication is handled by
              one of the `authBackend's. CAUTION this will leak information about private repos, the instance has
              access to. Information includes, but is not limited to, repository URLs, number and name of checks,
              and build logs
            '';
          };

          fullyPrivate = lib.mkOption {
            type = lib.types.submodule {
              options = {
                backend = lib.mkOption {
                  type = lib.types.enum [
                    "gitea"
                    "github"
                  ];
                };

                cookieSecretFile = lib.mkOption {
                  type = lib.types.path;
                  description = ''
                    Path to a file containing the cookie secret.
                  '';
                };

                clientSecretFile = lib.mkOption {
                  type = lib.types.path;
                  description = ''
                    Path to a file containing the client secret.
                  '';
                };

                clientId = lib.mkOption {
                  type = lib.types.str;
                  description = ''
                    Client secret used for OAuth2 authentication.
                  '';
                };

                teams = lib.mkOption {
                  type = lib.types.listOf lib.types.str;
                  description = ''
                    A list of teams that should be given access to BuildBot.
                  '';
                  default = [ ];
                };

                users = lib.mkOption {
                  type = lib.types.listOf lib.types.str;
                  description = ''
                    A list of users that should be given access to BuildBot.
                  '';
                  default = [ ];
                };

                port = lib.mkOption {
                  type = lib.types.port;
                  description = ''
                    Port number at which the `oauth2-proxy' will listen on.
                  '';
                  default = 8020;
                };
              };
            };
            description = ''
              Puts the buildbot instance behind `oauth2-proxy' which protects the whole instance. This makes
              buildbot-native authentication unnecessary unless one desires a mode where the team that can access
              the instance read-only is a superset of the the team that can access it read-write.
            '';
          };
        };
      };

      gitea = {
        enable = lib.mkEnableOption "Enable Gitea integration" // {
          default = cfg.authBackend == "gitea";
        };

        userAllowlist = lib.mkOption {
          type = lib.types.nullOr (lib.types.listOf lib.types.str);
          default = null;
          description = "If non-null, specifies users/organizations that are allowed to use buildbot, i.e. buildbot-nix will ignore any repositories not owned by these users/organizations.";
        };

        repoAllowlist = lib.mkOption {
          type = lib.types.nullOr (lib.types.listOf lib.types.str);
          default = null;
          description = "If non-null, specifies an explicit set of repositories that are allowed to use buildbot, i.e. buildbot-nix will ignore any repositories not in this list.";
        };

        tokenFile = lib.mkOption {
          type = lib.types.path;
          description = "Gitea token file";
        };
        webhookSecretFile = lib.mkOption {
          type = lib.types.path;
          description = "Gitea webhook secret file";
        };
        oauthSecretFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = "Gitea oauth secret file";
        };

        instanceUrl = lib.mkOption {
          type = lib.types.str;
          description = "Gitea instance URL";
        };
        oauthId = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Gitea oauth id. Used for the login button";
        };
        topic = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = "build-with-buildbot";
          description = ''
            Projects that have this topic will be built by buildbot.
            If null, all projects that the buildbot Gitea user has access to, are built.
          '';
        };

        sshPrivateKeyFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = ''
            If non-null the specified SSH key will be used to fetch all configured repositories.
          '';
        };

        sshKnownHostsFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = ''
            If non-null the specified known hosts file will be matched against when connecting to
            repositories over SSH.
          '';
        };
      };
      github = {
        enable = lib.mkEnableOption "Enable GitHub integration" // {
          default = cfg.authBackend == "github";
        };

        userAllowlist = lib.mkOption {
          type = lib.types.nullOr (lib.types.listOf lib.types.str);
          default = null;
          description = "If non-null, specifies users/organizations that are allowed to use buildbot, i.e. buildbot-nix will ignore any repositories not owned by these users/organizations.";
        };

        repoAllowlist = lib.mkOption {
          type = lib.types.nullOr (lib.types.listOf lib.types.str);
          default = null;
          description = "If non-null, specifies an explicit set of repositories that are allowed to use buildbot, i.e. buildbot-nix will ignore any repositories not in this list.";
        };

        authType = lib.mkOption {
          type = lib.types.attrTag {
            legacy = lib.mkOption {
              description = "GitHub legacy auth backend";
              type = lib.types.submodule {
                options.tokenFile = lib.mkOption {
                  type = lib.types.path;
                  description = "Github token file";
                };
              };
            };

            app = lib.mkOption {
              description = "GitHub legacy auth backend";
              type = lib.types.submodule {
                options.id = lib.mkOption {
                  type = lib.types.int;
                  description = ''
                    GitHub app ID.
                  '';
                };

                options.secretKeyFile = lib.mkOption {
                  type = lib.types.nullOr lib.types.path;
                  description = ''
                    GitHub app secret key file location.
                  '';
                };
              };
            };
          };
        };

        webhookSecretFile = lib.mkOption {
          type = lib.types.path;
          description = "Github webhook secret file";
        };
        oauthSecretFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = "Github oauth secret file";
        };
        # TODO: make this an option
        # https://github.com/organizations/numtide/settings/applications
        # Application name: BuildBot
        # Homepage URL: https://buildbot.numtide.com
        # Authorization callback URL: https://buildbot.numtide.com/auth/login
        # oauth_token:  2516248ec6289e4d9818122cce0cbde39e4b788d
        oauthId = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Github oauth id. Used for the login button";
        };
        topic = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = "build-with-buildbot";
          description = ''
            Projects that have this topic will be built by buildbot.
            If null, all projects that the buildbot github user has access to, are built.
          '';
        };
      };

      pullBased = {
        repositories = lib.mkOption {
          default = { };
          type = lib.types.attrsOf (
            lib.types.submodule {
              options = {
                defaultBranch = lib.mkOption {
                  type = lib.types.str;
                  description = ''
                    The repositories default branch.
                  '';
                };

                url = lib.mkOption {
                  type = lib.types.str;
                  description = ''
                    The repository's URL, must be fetchable by git.
                  '';
                };

                pollInterval = lib.mkOption {
                  type = lib.types.addCheck lib.types.int (x: x > 0);
                  default = cfg.pullBased.pollInterval;
                  description = ''
                    How often to poll this repository expressed in seconds.
                  '';
                };

                sshPrivateKeyFile = lib.mkOption {
                  type = lib.types.nullOr lib.types.path;
                  default = cfg.pullBased.sshPrivateKeyFile;
                  description = ''
                    If non-null the specified SSH key will be used to fetch all configured repositories.
                    This option is defaults to the global `sshPrivateKeyFile` option.
                  '';
                };

                sshKnownHostsFile = lib.mkOption {
                  type = lib.types.nullOr lib.types.path;
                  default = cfg.pullBased.sshKnownHostsFile;
                  description = ''
                    If non-null the specified known hosts file will be matched against when connecting to
                    repositories over SSH. This option defaults to the global `sshKnownHostsFile` option.
                  '';
                };
              };
            }
          );
        };

        pollInterval = lib.mkOption {
          type = lib.types.addCheck lib.types.int (x: x > 0);
          default = 60;
          description = ''
            How often to poll each repository by default expressed in seconds. This value can be overridden
            per repository.
          '';
        };

        pollSpread = lib.mkOption {
          type = lib.types.nullOr lib.types.int;
          default = null;
          description = ''
            If non-null and non-zero pulls will be randomly spread apart up to the specified
            number of seconds. Can be used to avoid a thundering herd situation.
          '';
        };

        sshPrivateKeyFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = ''
            If non-null the specified SSH key will be used to fetch all configured repositories.
            This option is overridden by the per-repository `sshPrivateKeyFile` option.
          '';
        };

        sshKnownHostsFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = ''
            If non-null the specified known hosts file will be matched against when connecting to
            repositories over SSH. This option is overridden by the per-repository `sshKnownHostsFile`
            option.
          '';
        };
      };

      admins = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = "Users that are allowed to login to buildbot, trigger builds and change settings";
      };

      workersFile = lib.mkOption {
        type = lib.types.path;
        description = "File containing a list of nix workers";
      };
      buildSystems = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ pkgs.stdenv.hostPlatform.system ];
        description = "Systems that we will be build";
      };
      evalMaxMemorySize = lib.mkOption {
        type = lib.types.int;
        default = 2048;
        description = ''
          Maximum memory size for nix-eval-jobs (in MiB) per
          worker. After the limit is reached, the worker is
          restarted.
        '';
      };
      evalWorkerCount = lib.mkOption {
        type = lib.types.nullOr lib.types.int;
        default = null;
        description = ''
          Number of nix-eval-jobs worker processes. If null, the number of cores is used.
          If you experience memory issues (buildbot-workers going out-of-memory), you can reduce this number.
        '';
      };
      domain = lib.mkOption {
        type = lib.types.str;
        description = "Buildbot domain";
        example = "buildbot.numtide.com";
      };

      webhookBaseUrl = lib.mkOption {
        type = lib.types.str;
        description = "URL base for the webhook endpoint that will be registered for github or gitea repos.";
        example = "https://buildbot-webhooks.numtide.com/";
        default = config.services.buildbot-master.buildbotUrl;
      };
      useHTTPS = lib.mkOption {
        type = lib.types.nullOr lib.types.bool;
        default = false;
        description = ''
          If buildbot is setup behind a reverse proxy other than the configured nginx set this to true
          to force the endpoint to use https:// instead of http://.
        '';
      };

      outputsPath = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        description = "Path where we store the latest build store paths names for nix attributes as text files. This path will be exposed via nginx at \${domain}/nix-outputs";
        default = null;
        example = "/var/www/buildbot/nix-outputs/";
      };

      jobReportLimit = lib.mkOption {
        type = lib.types.nullOr lib.types.ints.unsigned;
        description = ''
          The max number of build jobs per `nix-eval` `buildbot-nix` will report to backends (GitHub, Gitea, etc.).
          If set to `null`, report everything, if set to `n` (some unsiggned intereger), report builds individually
          as long as the number of builds is less than or equal to `n`, then report builds using a combined
          `nix-build-combined` build.
        '';
        default = 50;
      };

      effects.perRepoSecretFiles = lib.mkOption {
        type = lib.types.attrsOf lib.types.path;
        description = "Per-repository secrets files for buildbot effects. The attribute name is of the form \"github:owner/repo\". The secrets themselves need to be valid JSON files.";
        default = { };
        example = ''{ "github:nix-community/buildbot-nix" = config.agenix.secrets.buildbot-nix-effects-secrets.path; }'';
      };

      branches = lib.mkOption {
        type = lib.types.attrsOf (
          lib.types.submodule {
            options = {
              matchGlob = lib.mkOption {
                type = lib.types.str;
                description = ''
                  A glob specifying which branches to apply this rule to.
                '';
              };

              registerGCRoots = lib.mkOption {
                type = lib.types.bool;
                description = ''
                  Whether to register gcroots for branches matching this glob.
                '';
                default = true;
              };

              updateOutputs = lib.mkOption {
                type = lib.types.bool;
                description = ''
                  Whether to update outputs for branches matching this glob.
                '';
                default = false;
              };
            };
          }
        );
        default = { };
        description = ''
          An attrset of branch rules, each rule specifies which branches it should apply to using the
          `matchGlob` option and then the corresponding settings are applied to the matched branches.
          If multiple rules match a given branch, the rules are `or`-ed together, by `or`-ing each
          individual boolean option of all matching rules. Take the following as example:
          ```
             {
               rule1 = {
                 matchGlob = "f*";
                 registerGCRroots = false;
                 updateOutputs = false;
               }
               rule2 = {
                 matchGlob = "foo";
                 registerGCRroots = true;
                 updateOutputs = false;
               }
             }
          ```
          This example will result in `registerGCRoots` both being considered `true`,
          but `updateOutputs` being `false` for the branch `foo`.

          The default branches of all repos are considered to be matching of a rule setting all the options
          to `true`.
        '';
        example = lib.literalExpression ''
          {
            rule1 = {
              matchGlob = "f*";
              registerGCRroots = false;
              updateOutputs = false;
            }
            rule2 = {
              matchGlob = "foo";
              registerGCRroots = true;
              updateOutputs = false;
            }
          }
        '';
      };
    };
  };
  config = lib.mkIf cfg.enable {
    # By default buildbot uses a normal user, which is not a good default, because
    # we grant normal users potentially access to other resources. Also
    # we don't to be able to ssh into buildbot.

    users.users.buildbot = {
      isNormalUser = lib.mkForce false;
      isSystemUser = true;
    };

    assertions = [
      {
        assertion = lib.versionAtLeast packages.buildbot.version "4.0.0";
        message = ''
          `buildbot-nix` requires `buildbot` 4.0.0 or greater to function.
          Set services.buildbot-nix.packages.buildbot to a nixpkgs with buildbot >= 4.0.0,
          i.e. nixpkgs-unstable.
        '';
      }
      {
        assertion =
          cfg.authBackend == "github" -> (cfg.github.oauthId != null && cfg.github.oauthSecretFile != null);
        message = ''If config.services.buildbot-nix.master.authBackend is set to "github", then config.services.buildbot-nix.master.github.oauthId and config.services.buildbot-nix.master.github.oauthSecretFile have to be set.'';
      }
      {
        assertion =
          cfg.authBackend == "gitea" -> (cfg.gitea.oauthId != null && cfg.gitea.oauthSecretFile != null);
        message = ''config.services.buildbot-nix.master.authBackend is set to "gitea", then config.services.buildbot-nix.master.gitea.oauthId and config.services.buildbot-nix.master.gitea.oauthSecretFile have to be set.'';
      }
      {
        assertion = cfg.authBackend == "github" -> cfg.github.enable;
        message = ''
          If `cfg.authBackend` is set to `"github"` the GitHub backend must be enabled with `cfg.github.enable`;
        '';
      }
      {
        assertion = cfg.authBackend == "gitea" -> cfg.gitea.enable;
        message = ''
          If `cfg.authBackend` is set to `"gitea"` the GitHub backend must be enabled with `cfg.gitea.enable`;
        '';
      }
    ];

    services.buildbot-master = {
      enable = true;

      # disable example workers from nixpkgs
      builders = [ ];
      schedulers = [ ];
      workers = [ ];

      home = "/var/lib/buildbot";
      extraImports = ''
        from datetime import timedelta
        from buildbot_nix import (
          NixConfigurator,
          BuildbotNixConfig,
        )
        from pathlib import Path
        import json
      '';
      configurators = [
        ''
          util.JanitorConfigurator(logHorizon=timedelta(weeks=4), hour=12, dayOfWeek=6)
        ''
        ''
          NixConfigurator(
            BuildbotNixConfig.model_validate(json.loads(Path("${
              (pkgs.formats.json { }).generate "buildbot-nix-config.json" {
                db_url = cfg.dbUrl;
                auth_backend = cfg.authBackend;
                gitea =
                  if !cfg.gitea.enable then
                    null
                  else
                    {
                      user_allowlist = cfg.gitea.userAllowlist;
                      repo_allowlist = cfg.gitea.repoAllowlist;
                      token_file = "gitea-token";
                      webhook_secret_file = "gitea-webhook-secret";
                      project_cache_file = "gitea-project-cache.json";
                      oauth_secret_file = "gitea-oauth-secret";
                      instance_url = cfg.gitea.instanceUrl;
                      oauth_id = cfg.gitea.oauthId;
                      topic = cfg.gitea.topic;
                      ssh_private_key_file = cfg.gitea.sshPrivateKeyFile;
                      ssh_known_hosts_file = cfg.gitea.sshKnownHostsFile;
                    };
                github =
                  if !cfg.github.enable then
                    null
                  else
                    {
                      user_allowlist = cfg.github.userAllowlist;
                      repo_allowlist = cfg.github.repoAllowlist;
                      auth_type =
                        if (cfg.github.authType ? "legacy") then
                          { token_file = "github-token"; }
                        else if (cfg.github.authType ? "app") then
                          {
                            id = cfg.github.authType.app.id;
                            secret_key_file = "github-app-secret-key";
                            installation_token_map_file = "github-app-installation-token-map.json";
                            project_id_map_file = "github-app-project-id-map-name.json";
                            jwt_token_map = "github-app-jwt-token";
                          }
                        else
                          throw "authType is neither \"legacy\" nor \"app\"";
                      project_cache_file = "github-project-cache-v1.json";
                      webhook_secret_file = "github-webhook-secret";
                      oauth_secret_file = "github-oauth-secret";
                      oauth_id = cfg.github.oauthId;
                      topic = cfg.github.topic;
                    };
                pull_based =
                  if cfg.pullBased.repositories == [ ] then
                    null
                  else
                    {
                      repositories = lib.flip lib.mapAttrs cfg.pullBased.repositories (
                        name: repo: {
                          inherit name;
                          default_branch = repo.defaultBranch;
                          url = repo.url;
                          poll_interval = repo.pollInterval;
                          ssh_private_key_file = repo.sshPrivateKeyFile;
                          ssh_known_hosts_file = repo.sshKnownHostsFile;
                        }
                      );
                      poll_spread = cfg.pullBased.pollSpread;
                    };
                admins = cfg.admins;
                build_systems = cfg.buildSystems;
                eval_max_memory_size = cfg.evalMaxMemorySize;
                eval_worker_count = cfg.evalWorkerCount;
                domain = cfg.domain;
                use_https = cfg.useHTTPS;
                outputs_path = cfg.outputsPath;
                url = config.services.buildbot-nix.master.webhookBaseUrl;
                post_build_steps = cfg.postBuildSteps;
                job_report_limit = cfg.jobReportLimit;
                http_basic_auth_password_file = cfg.httpBasicAuthPasswordFile;
                effects_per_repo_secrets = lib.mapAttrs' (name: _path: {
                  inherit name;
                  value = "effects-secret__${cleanUpRepoName name}";
                }) cfg.effects.perRepoSecretFiles;
                branches = cfg.branches;
                nix_workers_secret_file = "buildbot-nix-workers";
              }
            }").read_text()))
          )
        ''
      ];
      buildbotUrl =
        let
          host = config.services.nginx.virtualHosts.${cfg.domain};
          hasSSL = host.forceSSL || host.addSSL || cfg.useHTTPS;
        in
        "${if hasSSL then "https" else "http"}://${cfg.domain}/";
      dbUrl = config.services.buildbot-nix.master.dbUrl;

      package = packages.buildbot;
      pythonPackages = ps: [
        (ps.toPythonModule packages.buildbot-worker)
        packages.buildbot-nix
        packages.buildbot-effects
        packages.buildbot-plugins.www
        packages.buildbot-gitea
      ];
    };

    systemd.services.buildbot-master = {
      after = [ "postgresql.service" ];
      path = [ pkgs.openssl ];
      serviceConfig = {
        # in master.py we read secrets from $CREDENTIALS_DIRECTORY
        LoadCredential =
          [ "buildbot-nix-workers:${cfg.workersFile}" ]
          ++ lib.optionals cfg.github.enable (
            [ "github-webhook-secret:${cfg.github.webhookSecretFile}" ]
            ++ lib.optional (
              cfg.github.authType ? "legacy"
            ) "github-token:${cfg.github.authType.legacy.tokenFile}"
            ++ lib.optional (
              cfg.github.authType ? "app"
            ) "github-app-secret-key:${cfg.github.authType.app.secretKeyFile}"
          )
          ++ lib.optional (cfg.authBackend == "gitea") "gitea-oauth-secret:${cfg.gitea.oauthSecretFile}"
          ++ lib.optional (cfg.authBackend == "github") "github-oauth-secret:${cfg.github.oauthSecretFile}"
          ++ lib.optionals cfg.gitea.enable [
            "gitea-token:${cfg.gitea.tokenFile}"
            "gitea-webhook-secret:${cfg.gitea.webhookSecretFile}"
          ]
          ++ lib.mapAttrsToList (
            repoName: path: "effects-secret__${cleanUpRepoName repoName}:${path}"
          ) cfg.effects.perRepoSecretFiles
          ++ lib.mapAttrsToList (
            repoName: repo: "pull-based__${cleanUpRepoName repoName}:${repo.sshPrivateKeyFile}"
          ) (lib.filterAttrs (_: repo: repo.sshPrivateKeyFile != null) cfg.pullBased.repositories);
        RuntimeDirectory = "buildbot-master";
      };
    };

    services.postgresql = {
      enable = true;
      ensureDatabases = [ "buildbot" ];
      ensureUsers = [
        {
          name = "buildbot";
          ensureDBOwnership = true;
        }
      ];
    };

    services.nginx.enable = true;
    services.nginx.virtualHosts.${cfg.domain} = {
      locations =
        {
          "/".proxyPass = "http://127.0.0.1:${builtins.toString backendPort}/";
          "/sse" = {
            proxyPass = "http://127.0.0.1:${builtins.toString backendPort}/sse";
            # proxy buffering will prevent sse to work
            extraConfig = "proxy_buffering off;";
          };
          "/ws" = {
            proxyPass = "http://127.0.0.1:${builtins.toString backendPort}/ws";
            proxyWebsockets = true;
            # raise the proxy timeout for the websocket
            extraConfig = "proxy_read_timeout 6000s;";
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

    systemd.tmpfiles.rules =
      lib.optional (cfg.outputsPath != null)
        # Allow buildbot-master to write to this directory
        "d ${cfg.outputsPath} 0755 buildbot buildbot - -";

    services.buildbot-nix.master.authBackend = lib.mkIf (
      cfg.accessMode ? "fullyPrivate"
    ) "httpbasicauth";

    services.oauth2-proxy = lib.mkIf (cfg.accessMode ? "fullyPrivate") {
      enable = true;

      clientID = cfg.accessMode.fullyPrivate.clientId;
      clientSecret = null;
      cookie.secret = null;

      extraConfig = lib.mkMerge [
        {
          config = "/etc/oauth2-proxy/oauth2-proxy.toml";
          redirect-url = "https://${cfg.domain}/oauth2/callback";

          http-address = "127.0.0.1:${builtins.toString cfg.accessMode.fullyPrivate.port}";

          upstream = "http://127.0.0.1:${builtins.toString config.services.buildbot-master.port}";

          cookie-secure = true;
          skip-auth-route = [ "^/change_hook" ];
          api-route = [
            "^/api"
            "^/ws$"
          ];
        }
        (lib.mkIf (cfg.authBackend == "httpbasicauth") { set-basic-auth = true; })
        (lib.mkIf
          (lib.elem cfg.accessMode.fullyPrivate.backend [
            "github"
            "gitea"
          ])
          {
            github-user = lib.concatStringsSep "," (cfg.accessMode.fullyPrivate.users ++ cfg.admins);
            github-team = cfg.accessMode.fullyPrivate.teams;
            email-domain = "*";
          }
        )
        (lib.mkIf (cfg.accessMode.fullyPrivate.backend == "github") { provider = "github"; })
        (lib.mkIf (cfg.accessMode.fullyPrivate.backend == "gitea") {
          provider = "github";
          provider-display-name = "Gitea";
          login-url = "${cfg.gitea.instanceUrl}/login/oauth/authorize";
          redeem-url = "${cfg.gitea.instanceUrl}/login/oauth/access_token";
          validate-url = "${cfg.gitea.instanceUrl}/api/v1/user/emails";
        })
      ];
    };

    systemd.services.oauth2-proxy = lib.mkIf (cfg.accessMode ? "fullyPrivate") {
      serviceConfig = {
        ConfigurationDirectory = "oauth2-proxy";
      };
      preStart = ''
        cat > $CONFIGURATION_DIRECTORY/oauth2-proxy.toml <<EOF
        client_secret = "$(cat ${cfg.accessMode.fullyPrivate.clientSecretFile})"
        cookie_secret = "$(cat ${cfg.accessMode.fullyPrivate.cookieSecretFile})"
        basic_auth_password = "$(cat ${cfg.httpBasicAuthPasswordFile})"
        # https://github.com/oauth2-proxy/oauth2-proxy/issues/1724
        scope = "read:user user:email repo"
        EOF
      '';
    };
  };
}
