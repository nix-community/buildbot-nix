{
  self,
  pkgs,
  ...
}:
let
  hci-effects = import ./effects-lib.nix { inherit pkgs; };
in
{ primaryRepo, ... }:
{
  onPush.default.outputs = {
    checks = self.checks.${pkgs.stdenv.hostPlatform.system};
    effects.deploy = hci-effects.runIf (primaryRepo.branch or null == "main") (
      hci-effects.mkEffect {
        effectScript = ''
          echo "${builtins.toJSON { inherit (primaryRepo) branch tag rev; }}"
          ${pkgs.hello}/bin/hello
        '';
      }
    );
  };
}
