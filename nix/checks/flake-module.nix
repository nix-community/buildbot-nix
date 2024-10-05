{ self, ... }:
{
  perSystem =
    { pkgs, lib, ... }:
    {
      checks =
        let
          # this gives us a reference to our flake but also all flake inputs
          checkArgs = {
            inherit self pkgs;
          };
        in
        lib.mkIf (pkgs.hostPlatform.isLinux) {
          master = import ./master.nix checkArgs;
          worker = import ./worker.nix checkArgs;
        };
    };
}
