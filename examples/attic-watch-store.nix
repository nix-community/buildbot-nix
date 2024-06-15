{ pkgs
, config
, ...
}: {
  # sops-nix (https://github.com/Mic92/sops-nix) is just an example, here.
  # Replace with your own secret management as needed: https://wiki.nixos.org/wiki/Comparison_of_secret_managing_schemes
  sops.secrets."attic/prod-auth-token" = { sopsFile = ../secrets.yaml; };
  sops.secrets."attic/netrc-file-pull-push" = { sopsFile = ../secrets.yaml; };

  # Add netrc file for this machine to do its normal thing with the cache, as a machine.
  nix.settings.netrc-file = config.sops.secrets."attic/netrc-file-pull-push".path;

  systemd.services.attic-watch-store = {
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    environment.HOME = "/var/lib/attic-watch-store";
    serviceConfig = {
      DynamicUser = true;
      MemoryHigh = "5%";
      MemoryMax = "10%";
      LoadCredential = "prod-auth-token:${config.sops.secrets."attic/prod-auth-token".path}";
      StateDirectory = "attic-watch-store";
    };
    path = [ pkgs.attic-client ];
    script = ''
      set -eux -o pipefail
      ATTIC_TOKEN=$(< $CREDENTIALS_DIRECTORY/prod-auth-token)
      # Replace https://cache.<domain> with your own cache URL.
      attic login prod https://cache.<domain> $ATTIC_TOKEN
      attic use prod
      exec attic watch-store prod:prod
    '';
  };
}       
