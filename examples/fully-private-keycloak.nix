{ pkgs, ... }:
{
  # For a more full fledged and commented example refer to ./master.nix and ./worker.nix,
  # those two files give a better introductory example than this one
  services.buildbot-nix.master = {
    enable = true;

    domain = "buildbot.example.org";
    workersFile = pkgs.writeText "workers.json" "changeMe";
    admins = [ ];

    # `authBackend` can be omitted here, the module sets it itself
    authBackend = "httpbasicauth";
    # this is a randomly generated secret, which is only used to authenticate requests
    # from the oauth2 proxy to buildbot
    httpBasicAuthPasswordFile = pkgs.writeText "http-basic-auth-passwd" "changeMe";

    gitea = {
      enable = true;
      tokenFile = "/secret/gitea_token";
      instanceUrl = "https://codeberg.org";
      webhookSecretFile = pkgs.writeText "webhook-secret" "changeMe";
      topic = "build-with-buildbot";
    };

    # optional nix-eval-jobs settings
    evalWorkerCount = 2; # limit number of concurrent evaluations
    evalMaxMemorySize = 4096; # limit memory usage per evaluation

    accessMode.fullyPrivate = {
      backend = "keycloak";
      # this is a randomly generated alphanumeric secret, which is used to encrypt the cookies set by
      # oauth2-proxy, it must be 8, 16, or 32 characters long
      cookieSecretFile = pkgs.writeText "github_cookie_secret" "changeMe";
      clientSecretFile = pkgs.writeText "github_oauth_secret" "changeMe";
      clientId = "Iv1.XXXXXXXXXXXXXXXX";

      "keycloak" = {
        oidcIssuerUrl = "https://auth.numtide.com/realms/numtide-internal";

        roles = [
          "numtiders"
        ];
      };
    };
  };

  services.buildbot-nix.worker = {
    enable = true;
    workerPasswordFile = "/secret/worker_secret";
  };
}
