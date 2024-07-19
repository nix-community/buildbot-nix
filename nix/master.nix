{ config
, pkgs
, lib
, ...
}:
let
  cfg = config.services.buildbot-nix.master;
  inherit (lib) mkRemovedOptionModule mkRenamedOptionModule;

  interpolateType =
    lib.mkOptionType {
      name = "interpolate";

      description = ''
        A type represnting a Buildbot interpolation string, supports interpolations like `result-%(prop:attr)s`.
      '';

      check = x:
        x ? "_type" && x._type == "interpolate" && x ? "value";
    };

  interpolateToString =
    value:
    if lib.isAttrs value && value ? "_type" && value._type == "interpolate" then
      "util.Interpolate(${builtins.toJSON value.value})"
    else
      builtins.toJSON value;
in
{
  imports = [
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
          "none"
        ];
        default = "github";
        description = ''
          Which OAuth2 backend to use.
        '';
      };
      buildRetries = lib.mkOption {
        type = lib.types.int;
        default = 1;
        description = "Number of times a build is retried";
      };

      postBuildSteps = lib.mkOption {
        default = [ ];
        description = ''
          A list of steps to execute after every successful build.
        '';
        type = lib.types.listOf (lib.types.submodule {
          options = {
            name = lib.mkOption {
              type = lib.types.str;
              description = ''
                The name of the build step, will show up in Buildbot's UI.
              '';
            };

            environment = lib.mkOption {
              type = with lib.types; attrsOf (oneOf [ interpolateType str ]);
              description = ''
                Extra environment variables to add to the environment of this build step.
                The base environment is the environment of the `buildbot-worker` service.

                To access the properties of a build, use the `interpolate` function defined in
                `inputs.buildbot-nix.lib.interpolate` like so `(interpolate "result-%(prop:attr)s")`.
              '';
              default = { };
            };

            command = lib.mkOption {
              type = with lib.types; oneOf [ str (listOf (oneOf [ str interpolateType ])) ];
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
        });

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

      cachix = {
        enable = lib.mkEnableOption "Enable Cachix integration";

        name = lib.mkOption {
          type = lib.types.str;
          description = "Cachix name";
        };

        signingKeyFile = lib.mkOption {
          type = lib.types.path;
          description = "Cachix signing key";
        };

        authTokenFile = lib.mkOption {
          type = lib.types.str;
          description = "Cachix auth token";
        };
      };
      gitea = {
        enable = lib.mkEnableOption "Enable Gitea integration" // {
          default = cfg.authBackend == "gitea";
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
      };
      github = {
        enable = lib.mkEnableOption "Enable GitHub integration" // {
          default = cfg.authBackend == "github";
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
        default = [ pkgs.hostPlatform.system ];
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

      buildbotNixpkgs = lib.mkOption {
        type = lib.types.raw;
        description = "Nixpkgs to use for buildbot packages";
      };

      outputsPath = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        description = "Path where we store the latest build store paths names for nix attributes as text files. This path will be exposed via nginx at \${domain}/nix-outputs";
        default = null;
        example = "/var/www/buildbot/nix-outputs";
      };
    };
  };
  config = lib.mkMerge [
    (lib.mkIf cfg.enable {
      # By default buildbot uses a normal user, which is not a good default, because
      # we grant normal users potentially access to other resources. Also
      # we don't to be able to ssh into buildbot.

      users.users.buildbot = {
        isNormalUser = lib.mkForce false;
        isSystemUser = true;
      };

      assertions = [
        {
          assertion =
            lib.versionAtLeast cfg.buildbotNixpkgs.buildbot.version "4.0.0";
          message = ''
            `buildbot-nix` requires `buildbot` 4.0.0 or greater to function.
            Set services.buildbot-nix.master.buildbotNixpkgs to a nixpkgs with buildbot >= 4.0.0,
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
          assertion =
            cfg.authBackend == "github" -> cfg.github.enable;
          message = ''
            If `cfg.authBackend` is set to `"github"` the GitHub backend must be enabled with `cfg.github.enable`;
          '';
        }
        {
          assertion =
            cfg.authBackend == "gitea" -> cfg.gitea.enable;
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
              BuildbotNixConfig.model_validate(json.loads(Path("${(pkgs.formats.json {}).generate "buildbot-nix-config.json" {
                db_url = cfg.dbUrl;
                auth_backend = cfg.authBackend;
                build_retries = cfg.buildRetries;
                cachix = if !cfg.cachix.enable then
                  null
                         else
                           {
                             name = cfg.cachix.name;
                             signing_key_file = cfg.cachix.signingKeyFile;
                             auth_token_file = cfg.cachix.authTokenFile;
                           };
                gitea = if !cfg.gitea.enable then
                  null
                        else
                          {
                            token_file = "gitea-token";
                            webhook_secret_file = "gitea-webhook-secret";
                            project_cache_file = "gitea-project-cache.json";
                            oauth_secret_file = "gitea-oauth-secret";
                            instance_url = cfg.gitea.instanceUrl;
                            oauth_id = cfg.gitea.oauthId;
                            topic = cfg.gitea.topic;
                          };
                github = if !cfg.github.enable then
                  null
                         else {
                           auth_type = if (cfg.github.authType ? "legacy") then
                             {
                               token_file = "github-token";
                             }
                                       else if (cfg.github.authType ? "app") then
                                         {
                                           id = cfg.github.authType.app.id;
                                           secret_key_file = cfg.github.authType.app.secretKeyFile;
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
                admins = cfg.admins;
                workers_file = cfg.workersFile;
                build_systems = cfg.buildSystems;
                eval_max_memory_size = cfg.evalMaxMemorySize;
                eval_worker_count = cfg.evalWorkerCount;
                domain = cfg.domain;
                webhook_base_url = cfg.webhookBaseUrl;
                use_https = cfg.useHTTPS;
                outputs_path = cfg.outputsPath;
                url = config.services.buildbot-nix.master.webhookBaseUrl;
                post_build_steps = cfg.postBuildSteps;
              }}").read_text()))
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

        package = cfg.buildbotNixpkgs.buildbot.overrideAttrs (old: {
          patches = old.patches ++ [ ./0001-master-reporters-github-render-token-for-each-reques.patch ];
        });
        pythonPackages =
          let
            buildbot-gitea = (cfg.buildbotNixpkgs.python3.pkgs.callPackage ./buildbot-gitea.nix {
              inherit (cfg.buildbotNixpkgs) buildbot;
            }).overrideAttrs (old: {
              patches = old.patches ++ [
                ./0002-GiteaHandler-set-branch-to-the-PR-branch-not-the-bas.patch
                ./0001-GiteaHandler-set-the-category-of-PR-changes-to-pull-.patch
              ];
            });
          in
          ps: [
            ps.pydantic
            pkgs.nix
            ps.requests
            ps.treq
            ps.psycopg2
            (ps.toPythonModule cfg.buildbotNixpkgs.buildbot-worker)
            cfg.buildbotNixpkgs.buildbot-plugins.www
            (cfg.buildbotNixpkgs.python3.pkgs.callPackage ../default.nix { })
            buildbot-gitea
          ];
      };

      systemd.services.buildbot-master = {
        after = [ "postgresql.service" ];
        path = [
          pkgs.openssl
        ];
        serviceConfig = {
          # in master.py we read secrets from $CREDENTIALS_DIRECTORY
          LoadCredential =
            [ "buildbot-nix-workers:${cfg.workersFile}" ]
            ++ lib.optionals cfg.github.enable ([
              "github-webhook-secret:${cfg.github.webhookSecretFile}"
            ]
            ++ lib.optional (cfg.github.authType ? "legacy")
              "github-token:${cfg.github.authType.legacy.tokenFile}"
            ++ lib.optional (cfg.github.authType ? "app")
              "github-app-secret-key:${cfg.github.authType.app.secretKeyFile}"
            )
            ++ lib.optional (cfg.authBackend == "gitea") "gitea-oauth-secret:${cfg.gitea.oauthSecretFile}"
            ++ lib.optional (cfg.authBackend == "github") "github-oauth-secret:${cfg.github.oauthSecretFile}"
            ++ lib.optionals cfg.cachix.enable [
              "cachix-signing-key:${builtins.toString cfg.cachix.signingKeyFile}"
              "cachix-auth-token:${builtins.toString cfg.cachix.authTokenFile}"
            ]
            ++ lib.optionals cfg.gitea.enable [
              "gitea-token:${cfg.gitea.tokenFile}"
              "gitea-webhook-secret:${cfg.gitea.webhookSecretFile}"
            ];
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
        locations = {
          "/".proxyPass = "http://127.0.0.1:${builtins.toString config.services.buildbot-master.port}/";
          "/sse" = {
            proxyPass = "http://127.0.0.1:${builtins.toString config.services.buildbot-master.port}/sse";
            # proxy buffering will prevent sse to work
            extraConfig = "proxy_buffering off;";
          };
          "/ws" = {
            proxyPass = "http://127.0.0.1:${builtins.toString config.services.buildbot-master.port}/ws";
            proxyWebsockets = true;
            # raise the proxy timeout for the websocket
            extraConfig = "proxy_read_timeout 6000s;";
          };
        } // lib.optionalAttrs (cfg.outputsPath != null) { "/nix-outputs".root = cfg.outputsPath; };
      };

      systemd.tmpfiles.rules =
        [
          # delete legacy gcroot location, can be dropped after 2024-06-01
          "R /var/lib/buildbot-worker/gcroot - - - - -"
        ]
        ++ lib.optional (cfg.outputsPath != null)
          # Allow buildbot-master to write to this directory
          "d ${cfg.outputsPath} 0755 buildbot buildbot - -";
    })
  ];
}
