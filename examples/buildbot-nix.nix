{ pkgs, ... }:
{
  services.buildbot-nix = {
    enable = true;
    # Domain name under which the web frontend is reachable.
    domain = "buildbot2.thalheim.io";
    # Users in this list are allowed to trigger builds and change settings.
    # Entries are provider-qualified: "github:<login>", "gitea:<login>",
    # "oidc:<issuer>:<username>".
    admins = [ "github:Mic92" ];
    github = {
      enable = true;
      # GitHub App configuration.
      appId = 0; # FIXME: replace with the App ID obtained from GitHub
      appSecretKeyFile = pkgs.writeText "app-secret.key" "00000000000000000000"; # FIXME: replace with the App private key; use a secret manager
      # The webhook secret configured in the GitHub App settings.
      webhookSecretFile = pkgs.writeText "webhookSecret" "00000000000000000000"; # FIXME: use a secret manager
      # OAuth credentials for the login button (from the same GitHub App).
      oauthId = "aaaaaaaaaaaaaaaaaaaa";
      oauthSecretFile = pkgs.writeText "oauthSecret" "ffffffffffffffffffffffffffffffffffffffff"; # FIXME: use a secret manager
      # All repositories with this topic will be built.
      topic = "buildbot-mic92";
    };

    # Gitea example.
    # gitea = {
    #   enable = true;
    #   instanceUrl = "https://git.clan.lol";
    #   tokenFile = "/var/lib/secrets/gitea-token";
    #   oauthId = "aaaaaaaaaaaaaaaaaaaa";
    #   oauthSecretFile = "/var/lib/secrets/gitea-oauth-secret";
    #   topic = "build-with-buildbot";
    # };

    # Systems to build; everything else arrives via nix remote builders.
    buildSystems = [ "x86_64-linux" ];

    # Optional nix-eval-jobs settings.
    evalWorkerCount = 2; # limit the number of concurrent evaluation workers
    evalMaxMemorySize = 4096; # per-worker memory limit in MiB

    # By default only default branches build. Additional branches:
    # branches = {
    #   releaseBranches.matchGlob = "release-*";
    # };

    # Generic OIDC login example.
    # oidc = {
    #   enable = true;
    #   name = "Provider Name";
    #   discoveryUrl = "https://id.thalheim.io/.well-known/openid-configuration";
    #   clientId = "aaaaaaaaaaaaaaaaaaaa";
    #   clientSecretFile = "/var/lib/secrets/oidc-secret";
    # };

    # Allow unauthenticated users to cancel and restart builds, e.g.
    # behind a VPN where network access implies trust.
    # allowUnauthenticatedControl = true;

    # Local PostgreSQL over the unix socket is provisioned by default.
    # Remote database instead:
    # database.createLocally = false;
    # database.urlFile = "/var/lib/secrets/db-url"; # postgresql://user:pass@host/db

    # Request a TLS certificate for the nginx virtual host (recommended).
    nginx.enableACME = true;
  };
}
