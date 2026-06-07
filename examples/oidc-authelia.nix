{ pkgs, config, ... }:
{
  services.buildbot-nix = {
    # Admin entries for OIDC users are qualified as
    # "oidc:<issuer-host>:<preferred_username>".
    admins = [ "oidc:auth.thalheim.io:alice" ];
    oidc = {
      enable = true;
      name = "Authelia";
      discoveryUrl = "https://auth.thalheim.io/.well-known/openid-configuration";
      clientId = "buildbot";
      clientSecretFile = pkgs.writeText "oidc-secret" "00000000000000000000"; # FIXME: use a secret manager
    };
  };

  services.authelia.instances.main = {
    enable = true;

    secrets = {
      jwtSecretFile = pkgs.writeText "jwt-secret" "00000000000000000000"; # FIXME: use a secret manager
      storageEncryptionKeyFile = pkgs.writeText "storage-key" "00000000000000000000"; # FIXME: use a secret manager
      sessionSecretFile = pkgs.writeText "session-secret" "00000000000000000000"; # FIXME: use a secret manager
      # The OIDC provider needs an HMAC secret and an RSA issuer key:
      #   openssl rand -base64 64 > hmac-secret
      #   openssl genrsa 4096 > issuer-key
      oidcHmacSecretFile = pkgs.writeText "oidc-hmac" "00000000000000000000"; # FIXME: use a secret manager
      oidcIssuerPrivateKeyFile = "/var/lib/secrets/authelia-issuer-key"; # FIXME: use a secret manager
    };

    settings = {
      authentication_backend.file.path = "/var/lib/authelia-main/users.yml";
      storage.local.path = "/var/lib/authelia-main/db.sqlite3";
      notifier.filesystem.filename = "/var/lib/authelia-main/notifications.txt";
      session.cookies = [
        {
          domain = "thalheim.io";
          authelia_url = "https://auth.thalheim.io";
        }
      ];

      identity_providers.oidc.clients = [
        {
          client_id = "buildbot";
          client_name = "Buildbot";
          # Authelia stores only a digest of the client secret; hash the
          # plaintext from clientSecretFile above with:
          #   authelia crypto hash generate pbkdf2 --password <secret>
          client_secret = "$pbkdf2-sha512$310000$0000000000000000$00000000000000000000000000000000000000000000000000000000000000000000000000000000000000"; # FIXME
          redirect_uris = [ "https://${config.services.buildbot-nix.domain}/auth/oidc/callback" ];
          scopes = [
            "openid"
            "email"
            "profile"
          ];
          authorization_policy = "one_factor";
        }
      ];
    };
  };
}
