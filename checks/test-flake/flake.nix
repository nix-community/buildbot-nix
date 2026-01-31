{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    hercules-ci-effects.url = "github:hercules-ci/hercules-ci-effects";
  };

  outputs =
    {
      self,
      nixpkgs,
      hercules-ci-effects,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems =
        f:
        builtins.listToAttrs (
          map (system: {
            name = system;
            value = f system;
          }) systems
        );
    in
    {
      checks = forAllSystems (system: { });

      herculesCI =
        args:
        let
          pkgs = nixpkgs.legacyPackages.x86_64-linux;
          hci-effects = hercules-ci-effects.lib.withPkgs pkgs;
        in
        {
          onPush.default.outputs.effects = { };

          onSchedule = {
            flake-update = {
              when = {
                hour = 4;
                minute = 0;
                dayOfWeek = [ "Mon" ];
              };
              outputs.effects.update = hci-effects.mkEffect {
                effectScript = ''
                  echo "Flake update executed!"
                '';
              };
            };
          };
        };
    };
}
