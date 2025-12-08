{ pkgs, ... }:
{
  # This example demonstrates how to use buildbot-nix with a generic OIDC provider
  # such as Dex, Keycloak, Authentik, or any other OIDC-compatible identity provider.
  #
  # For a more complete example with GitHub or Gitea, see ./master.nix and ./worker.nix

  services.buildbot-nix.master = {
    enable = true;

    domain = "buildbot.example.org";
    workersFile = pkgs.writeText "workers.json" ''
      [
        { "name": "worker1", "pass": "worker-password-change-me", "cores": 4 }
      ]
    '';
    admins = [ "admin-user@example.com" ];

    # When using fullyPrivate mode with OIDC, the authBackend is automatically set to "httpbasicauth"
    # by the module, so you don't need to set it explicitly.

    # This is a randomly generated secret (16, 24, or 32 bytes), which is only used to authenticate
    # requests from the oauth2 proxy to buildbot
    httpBasicAuthPasswordFile = pkgs.writeText "http-basic-auth-passwd" "change-me-random-secret";

    # Configure your Git forge integration (GitHub, Gitea, or pull-based)
    # This example uses Gitea, but you can use GitHub or configure repositories manually
    gitea = {
      enable = true;
      tokenFile = pkgs.writeText "gitea_token" "change-me-gitea-token";
      instanceUrl = "https://git.example.com";
      webhookSecretFile = pkgs.writeText "webhook-secret" "change-me-webhook-secret";
      topic = "build-with-buildbot";
    };

    # Alternatively, use GitHub:
    # github = {
    #   enable = true;
    #   webhookSecretFile = pkgs.writeText "github_webhook_secret" "change-me";
    #   topic = "build-with-buildbot";
    #   appSecretKeyFile = pkgs.writeText "github_app_secret_key" "change-me";
    #   appId = 123456;
    # };

    # Optional nix-eval-jobs settings
    evalWorkerCount = 2; # limit number of concurrent evaluations
    evalMaxMemorySize = 4096; # limit memory usage per evaluation

    # Configure fully private mode with generic OIDC
    accessMode.fullyPrivate = {
      backend = "oidc";

      # OAuth2 client credentials (obtained from your OIDC provider)
      clientId = "buildbot-nix-client-id";
      clientSecretFile = pkgs.writeText "oidc_client_secret" "change-me-client-secret";

      # Cookie secret for oauth2-proxy (must be 16, 24, or 32 characters)
      cookieSecretFile = pkgs.writeText "cookie_secret" "change-me-16-chars!";

      # OIDC-specific configuration
      oidc = {
        # The issuer URL of your OIDC provider
        # Examples:
        #   - Dex: "https://dex.example.com"
        #   - Keycloak: "https://keycloak.example.com/realms/my-realm"
        #   - Authentik: "https://authentik.example.com/application/o/buildbot/"
        issuerUrl = "https://dex.example.com";

        # List of OIDC groups that should have access to BuildBot
        # Users must be a member of at least one of these groups
        # Leave empty to allow all authenticated users
        allowedGroups = [
          "buildbot-users"
          "developers"
        ];

        # List of email domains to allow. Use ["*"] to allow all domains.
        emailDomains = [ "*" ];
        # Or restrict to specific domains:
        # emailDomains = [ "example.com" "trusted-partner.com" ];
      };
    };
  };

  services.buildbot-nix.worker = {
    enable = true;
    workerPasswordFile = pkgs.writeText "worker_secret" "worker-password-change-me";
  };
}
