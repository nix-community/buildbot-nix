{
  self,
  pkgs,
  hercules-ci-effects,
  ...
}:
let
  hci-effects = hercules-ci-effects.lib.withPkgs pkgs;
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
