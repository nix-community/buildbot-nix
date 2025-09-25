(import ./lib.nix) {
  name = "master";
  nodes = {
    # `self` here is set by using specialArgs in `lib.nix`
    node1 =
      { self, pkgs, ... }:
      {
        imports = [ self.nixosModules.buildbot-master ];
        services.buildbot-nix.master = {
          enable = true;
          domain = "buildbot2.thalheim.io";
          workersFile = pkgs.writeText "workers.json" ''
            [
              { "name": "eve", "pass": "XXXXXXXXXXXXXXXXXXXX", "cores": 16 }
            ]
          '';
          admins = [ "Mic92" ];
          github = {
            appId = 123456; # Example app ID for testing
            appSecretKeyFile = pkgs.writeText "app-secret.key" ''
              -----BEGIN PRIVATE KEY-----
              MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQDPJsoxeXCcPuSm
              54EtmlpNMOZTDJ8JDKCv2iKzlBATukfAewdQPlPH5Fe94oJ1I5/TuTWc+2D9PzW8
              8IzcXxlr5mPTchy8Ag9W2JTr3rarDkZxN9amHCqAyxT78xl3C8nfD5FVyNN1I9dr
              ht7crKIrRp7oBX08II9iKpUE4T6IiNk42BLNdGqcJQITn0t5FHdtt5e96BnarNcF
              ridbiSpfFCSLbU87m9KXHnIZXqMv5smExTakw++90A0PgMVxFqlrUysg0PDTyvQF
              RweOkacUbN8vhIlxkT5ltX+sb6oSQpTA0/atn5KevUUDuHMcs9e7kIKRWYsQkq7t
              MvWkUT4FAgMBAAECggEAHLhZzaCy6HK+1FYh/o7ZKL0YHdkQ3poR2Dez2nZzgSyx
              QII+KhWuG3dw48p5AGEjSmEyCfT/RjVCj9LBENediGxtmDYIvldBxa5q/UXISTCG
              OzG88JRUnz0oyGK0u+DWSPcZVQ2uJZ5Fwmp1UR6dMEdPBkemjJTOFFPni8Dn4Nlq
              esZFRBWt43N9PEuJIjHyj8bmd3ySpL1H95yQr1efIDOf9A6+rXxKgxNiMc40P0QC
              /O/m66ssvWHH6gLt8jGl4QRavp8wzYuPhAEqamBYw/xjaObNkZSF8oKA251MpUfa
              uB/vDHzmISHFTStiZjCAFrsAPiU9DYXEUF91ZfRgMQKBgQDoZVjlbI1m8I1rRpgx
              DDAabr67EvvES8adIR3yOwxY5B5UDEStFdBphHmJ2ofqeuUX+V634SVHkxb1y8rN
              iywqYpznV8i5a6ILdtQ7/OAatys0QJ+vBvMc9BmGgJ7WgKB1lHgXk43T/Attrap6
              FeBVmXa7e7RgEf0tl2DYNs6EVQKBgQDkMQzNTsmr4dE6jL14qZA79INwKf7uxq4G
              wl2cqd1S3MDw1CtlGBwWiFMfj4aylHjQoSIMpD6tHCKWuXaXVXLr/niVZKmYPimg
              Cmdfac4kL9z32SiOpQVN/mJeC+Hy/PiKMckB4NsFAeKFe+pxXyfpPsI0icJ2uiAM
              8yHR4WYC8QKBgHC4l8HQQVXo3+9ksnU34C0yAjljH9M6nf+hDJFtqrODEmLaAIWj
              yw8jPoBrCvnk2jIitpqiDh8FbWGTk67XDnkQk+JyZd3qIxNEc/UU1u6eYcpafhm7
              WTh1/duLj3+jrDDb7tQgse5cln6Aeev1qHZclYainf7rOs5eWo8FJm5xAoGBANoK
              vG7ZX/7rUd+eZ9WKQJXpeEaO+lfyZIt04bo23ZK1+W6lbam1tfEZ5kN8A3tUP3Uq
              4rwtnO4QukRHhzfnoF4708D8ZMlibKfOCSS0lxMg4QW67PQQXtc9wYSX2hky+9Ig
              7C7tSpqoSGjAFS6rfBl1rGBDWhvUkZeOIrzHoZAhAoGBAM2/it5/qAGocB6TZcbe
              BqVkm/Po+z3qXQQF2dG+pWYG0NKPfP6CemJnB/gc1DY43kb+BMLMWQsa9IA5XfGh
              Z0VvcNg2m19AalhdkYk5HE3vvMfv3jV7pW45q4tQdofiOo1sfH6CeadyOg8MfO21
              uLPlDssCZjYKhdDkSqrUtpcq
              -----END PRIVATE KEY-----
            ''; # Test RSA private key
            webhookSecretFile = pkgs.writeText "webhookSecret" "00000000000000000000";
            oauthSecretFile = pkgs.writeText "oauthSecret" "ffffffffffffffffffffffffffffffffffffffff";
            oauthId = "aaaaaaaaaaaaaaaaaaaa";
          };
          cachix = {
            enable = true;
            name = "my-cachix";
            auth.authToken.file = pkgs.writeText "cachixAuthToken" "00000000000000000000";
          };
        };
      };
  };
  # This is the test code that will check if our service is running correctly:
  testScript = ''
    start_all()
    # wait for our service to start
    node1.wait_for_unit("buildbot-master")
    node1.wait_until_succeeds("curl --fail -s --head localhost:8010", timeout=60)
  '';
}
