{ inputs, ... }:
{
  lib,
  clanLib,
  ...
}:
{
  _class = "clan.service";

  manifest.name = "@numtide/buildbot";
  roles."master" = {
    interface =
      { config, ... }:
      {
        imports = [
          ./common-options.nix
        ];

        config = {
          authBackend = lib.mkIf (config.accessMode ? "fullyPrivate") "httpbasicauth";
        };
      };

    perInstance =
      {
        settings,
        roles,
        instanceName,
        ...
      }:
      {
        nixosModule =
          { config, pkgs, ... }:
          let
            buildbot-nix = config.services.buildbot-nix;
          in
          {
            imports = [
              inputs.self.nixosModules.buildbot-master
              inputs.self.nixosModules.buildbot-worker
            ];

            clan.core.vars.generators."buildbot-nix" = {
              files."worker-password" = { };
              files."worker-count" = {
                secret = false;
              };
              files."workers" = { };

              prompts."worker-count" = {
                persist = true;
                type = "line";
                description = ''
                  Number of workers
                '';
              };

              runtimeInputs = [
                pkgs.xkcdpass
              ];

              script = ''
                _worker_count=$(cat "''${prompts}/worker-count")

                xkcdpass -n 8 -d - > "$out/worker-password"
                cat > "$out/workers" <<-EOF
                [{ "name": "buildbot-ntd-one", "pass": "$(cat "$out/worker-password")", "cores": "$_worker_count" }]
                EOF
              '';
            };

            clan.core.vars.generators."buildbot-nix-gitea" = lib.mkIf settings.gitea.enable {
              files."webhook-secret" = { };
              files."password" = { };

              prompts."token" = {
                persist = true;
                type = "hidden";
                description = ''
                  Token used for authenticating with Gitea.
                '';
              };

              runtimeInputs = [
                pkgs.xkcdpass
              ];

              script = ''
                xkcdpass -n 8 -d - > "$out/webhook-secret"
                xkcdpass -n 8 -d - > "$out/password"
              '';
            };

            clan.core.vars.generators."buildbot-nix-gitea-oauth" =
              lib.mkIf
                (
                  settings.accessMode ? "fullyPrivate" && settings.accessMode.fullyPrivate.backend == "gitea"
                  || settings.authBackend == "gitea"
                )
                {
                  files."oauth-id" = {
                    secret = false;
                  };

                  prompts."oauth-id" = {
                    persist = true;
                    type = "line";
                    description = ''
                      OAuth2 ID used for authenticating with Gitea as a OAuth2 client.
                    '';
                  };

                  prompts."oauth-secret" = {
                    persist = true;
                    type = "hidden";
                    description = ''
                      OAuth2 secret used for authenticating with Gitea as a OAuth2 client.
                    '';
                  };
                };

            clan.core.vars.generators."buildbot-nix-fullyPrivate" =
              lib.mkIf (settings.accessMode ? "fullyPrivate")
                {
                  files."cookie-secret" = { };
                  files."basic-auth-secret" = { };

                  runtimeInputs = [
                    pkgs.xkcdpass
                  ];

                  script = ''
                    xkcdpass -n 8 -d - > "$out/basic-auth-secret"
                    xkcdpass -n 8 -d - | head -c 32 > "$out/cookie-secret"
                  '';
                };

            clan.core.vars.generators."buildbot-nix-github-oauth" =
              lib.mkIf
                (
                  settings.accessMode ? "fullyPrivate" && settings.accessMode.fullyPrivate.backend == "github"
                  || settings.authBackend == "github"
                )
                {
                  files."oauth-id" = {
                    secret = false;
                  };

                  prompts."oauth-id" = {
                    persist = true;
                    type = "line";
                    description = ''
                      OAuth2 ID used for authenticating with GitHub as a OAuth2 client.
                    '';
                  };
                  prompts."oauth-secret" = {
                    persist = true;
                    type = "hidden";
                    description = ''
                      OAuth2 secret used for authenticating with GitHub as a OAuth2 client.
                    '';
                  };
                };

            clan.core.vars.generators."buildbot-nix-github" = lib.mkIf settings.github.enable {
              files."app-id" = {
                secret = false;
              };
              files."webhook-secret" = { };

              prompts."app-id" = {
                persist = true;
                type = "line";
                description = ''
                  Application ID to authenticate as.
                '';
              };
              prompts."app-secret" = {
                persist = true;
                type = "multiline-hidden";
                description = ''
                  Application secret used for authenticating with GitHub as a application.
                '';
              };

              runtimeInputs = [
                pkgs.xkcdpass
              ];

              script = ''
                xkcdpass -n 8 -d - > "$out/webhook-secret"
              '';
            };

            clan.core.vars.generators."buildbot-nix-cachix" = lib.mkIf settings.cachix.enable {
              prompts."token" = {
                persist = true;
                type = "hidden";
                description = ''
                  Token used to authenticate with Cachix.
                '';
              };
            };

            systemd.services."oauth2-proxy".enableStrictShellChecks = false;

            services.buildbot-nix.master = {
              enable = true;
              inherit (settings)
                admins
                domain
                evalWorkerCount
                ;
              buildSystems =
                if settings.buildSystems == [ ] then [ pkgs.stdenv.hostPlatform.system ] else settings.buildSystems;
              workersFile = config.clan.core.vars.generators."buildbot-nix".files."workers".path;

              authBackend = lib.mkDefault settings.authBackend;

              httpBasicAuthPasswordFile = lib.mkIf (
                settings.accessMode ? "fullyPrivate"
              ) config.clan.core.vars.generators."buildbot-nix-fullyPrivate".files."basic-auth-secret".path;

              accessMode = lib.mkIf (settings.accessMode ? "fullyPrivate") {
                fullyPrivate = {
                  inherit (settings.accessMode.fullyPrivate) backend teams;

                  cookieSecretFile =
                    config.clan.core.vars.generators."buildbot-nix-fullyPrivate".files."cookie-secret".path;
                  clientSecretFile =
                    {
                      "gitea" = config.clan.core.vars.generators."buildbot-nix-gitea-oauth".files."oauth-secret".path;
                      "github" = config.clan.core.vars.generators."buildbot-nix-github-oauth".files."oauth-secret".path;
                    }
                    .${settings.accessMode.fullyPrivate.backend};
                  clientId = builtins.readFile (
                    {
                      "gitea" = config.clan.core.vars.generators."buildbot-nix-gitea-oauth".files."oauth-id".path;
                      "github" = config.clan.core.vars.generators."buildbot-nix-github-oauth".files."oauth-id".path;
                    }
                    .${settings.accessMode.fullyPrivate.backend}
                  );
                };
              };

              cachix = lib.mkIf settings.cachix.enable {
                enable = true;
                inherit (settings.cachix) name;
                auth.authToken.file = config.clan.core.vars.generators."buildbot-nix-cachix".files."token".path;
              };

              niks3 = lib.mkIf settings.niks3.enable {
                enable = true;
                inherit (settings.niks3) serverUrl;

                authTokenFile = config.clan.core.vars.generators.niks3-api-token.files."token".path;
                package = inputs.niks3.packages.${pkgs.system}.default;
              };

              gitea = lib.mkIf settings.gitea.enable {
                enable = true;
                tokenFile = config.clan.core.vars.generators."buildbot-nix-gitea".files."token".path;
                webhookSecretFile =
                  config.clan.core.vars.generators."buildbot-nix-gitea".files."webhook-secret".path;

                inherit (settings.gitea)
                  instanceUrl
                  topic
                  ;
              };
              github = lib.mkIf settings.github.enable {
                enable = true;
                appId = lib.importJSON config.clan.core.vars.generators."buildbot-nix-github".files."app-id".path;
                appSecretKeyFile = config.clan.core.vars.generators."buildbot-nix-github".files."app-secret".path;
                webhookSecretFile =
                  config.clan.core.vars.generators."buildbot-nix-github".files."webhook-secret".path;
                oauthSecretFile =
                  config.clan.core.vars.generators."buildbot-nix-github-oauth".files."oauth-secret".path;
                oauthId =
                  builtins.readFile
                    config.clan.core.vars.generators."buildbot-nix-github-oauth".files."oauth-id".path;
                inherit (settings.github) topic;
              };
              outputsPath = "/var/www/buildbot/nix-outputs";
            };

            services.telegraf.extraConfig.inputs.prometheus.urls = [ "http://localhost:8011/metrics" ];

            services.buildbot-master = {
              extraConfig = ''
                c['services'].append(reporters.Prometheus(port=8011))
              '';
              pythonPackages = ps: [
                (ps.buildPythonPackage rec {
                  pname = "buildbot-prometheus";
                  version = "0c81a89bbe34628362652fbea416610e215b5d1e";
                  src = pkgs.fetchFromGitHub {
                    owner = "claws";
                    repo = "buildbot-prometheus";
                    rev = version;
                    hash = "sha256-bz2Nv2RZ44i1VoPvQ/XjGMfTT6TmW6jhEVwItPk23SM=";
                  };
                  format = "setuptools";
                  propagatedBuildInputs = [ ps.prometheus-client ];
                  doCheck = false;
                })
              ];
            };

            services.buildbot-nix.worker = {
              enable = true;
              workerPasswordFile = config.clan.core.vars.generators."buildbot-nix".files."worker-password".path;
              workers = lib.importJSON config.clan.core.vars.generators."buildbot-nix".files."worker-count".path;
            };

            services.nginx.virtualHosts."${settings.domain}" = {
              forceSSL = true;
              enableACME = true;
            };
          };
      };
  };
}
