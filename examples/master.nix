{ pkgs, ... }:
{
  services.buildbot-nix.master = {
    enable = true;
    # Domain name under which the buildbot frontend is reachable
    domain = "buildbot2.thalheim.io";
    # The workers file configures credentials for the buildbot workers to connect to the master.
    # "name" is the configured worker name in services.buildbot-nix.worker.name of a worker
    # (defaults to the hostname of the machine)
    # "pass" is the password for the worker configured in `services.buildbot-nix.worker.workerPasswordFile`
    # "cores" is the number of cpu cores the worker has.
    # The number must match the actual core count of the machine as otherwise not enough buildbot-workers are created.
    workersFile = pkgs.writeText "workers.json" ''
      [
        { "name": "eve", "pass": "XXXXXXXXXXXXXXXXXXXX", "cores": 16 }
      ]
    ''; # FIXME: replace this with a secret not stored in the nix store
    # Users in this list will be able to reload the project list.
    # All other user in the organization will be able to restart builds or evaluations.
    admins = [ "Mic92" ];
    github = {
      # Use this when you have set up a GitHub App
      authType.app = {
        id = 000000; # FIXME: replace with App ID obtained from GitHub
        secretKeyFile = pkgs.writeText "app-secret.key" "00000000000000000000"; # FIXME: replace with App secret key obtained from GitHub
      };
      #authType.legacy = {
      #  # Github user token used as a CI identity
      #  tokenFile = pkgs.writeText "github-token" "ghp_000000000000000000000000000000000000"; # FIXME: replace this with a secret not stored in the nix store
      #};
      # A random secret used to verify incoming webhooks from GitHub
      # buildbot-nix will set up a webhook for each project in the organization
      webhookSecretFile = pkgs.writeText "webhookSecret" "00000000000000000000"; # FIXME: replace this with a secret not stored in the nix store
      # Either create a GitHub app or an OAuth app
      # After creating the app, press "Generate a new client secret" and fill in the client ID and secret below
      oauthId = "aaaaaaaaaaaaaaaaaaaa";
      oauthSecretFile = pkgs.writeText "oauthSecret" "ffffffffffffffffffffffffffffffffffffffff"; # FIXME: replace this with a secret not stored in the nix store
      # All github projects with this topic will be added to buildbot.
      # One can trigger a project scan by visiting the Builds -> Builders page and looking for the "reload-github-project" builder.
      # This builder has a "Update Github Projects" button that everyone in the github organization can use.
      topic = "buildbot-mic92";
    };

    # Gitea example
    # authBackend = "gitea"; # login with gitea
    #gitea = {
    #  enable = true;
    #  instanceUrl = "https://git.clan.lol";
    #  # Create a Gitea App with for redirect uris: https://buildbot.clan.lol/auth/login
    #  oauthId = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    #  oauthSecretFile = pkgs.writeText "gitea-oauth-secret" "ffffffffffffffffffffffffffffffffffffffff"; # FIXME: replace this with a secret not stored in the nix store;
    #  webhookSecretFile = pkgs.writeText "gitea-webhook-secret" "00000000000000000000"; # FIXME: replace this with a secret not stored in the nix store
    #  tokenFile = pkgs.writeText "gitea-token" "0000000000000000000000000000000000000000"; # FIXME: replace this with a secret not stored in the nix store
    #  topic = "buildbot-clan";
    #};
    # optional expose latest store path as text file
    # outputsPath = "/var/www/buildbot/nix-outputs";

    # optional nix-eval-jobs settings
    # evalWorkerCount = 8; # limit number of concurrent evaluations
    # evalMaxMemorySize = "2048"; # limit memory usage per evaluation

    # optional cachix
    #cachix = {
    #  name = "my-cachix";
    #  # One of the following is required:
    #  signingKeyFile = "/var/lib/secrets/cachix-key";
    #  authTokenFile = "/var/lib/secrets/cachix-token";
    #};
  };

  # Optional: Enable acme/TLS in nginx (recommended)
  #services.nginx.virtualHosts.${config.services.buildbot-nix.master.domain} = {
  #  forceSSL = true;
  #  enableACME = true;
  #};

  # Optional: If buildbot is setup to run behind another proxy that does TLS
  # termination set this to true to have buildbot use https:// for its endpoint
  #useHTTPS = true;
}
