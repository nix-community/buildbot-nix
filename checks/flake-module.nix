{ self, ... }:
{
  perSystem =
    {
      self',
      system,
      pkgs,
      lib,
      ...
    }:
    {
      checks =
        let
          # this gives us a reference to our flake but also all flake inputs
          checkArgs = {
            inherit self pkgs;
          };
          nixosMachines = lib.mapAttrs' (
            name: config: lib.nameValuePair "nixos-${name}" config.config.system.build.toplevel
          ) ((lib.filterAttrs (name: _: lib.hasSuffix system name)) self.nixosConfigurations);
          packages = lib.mapAttrs' (n: lib.nameValuePair "package-${n}") self'.packages;
          devShells = lib.mapAttrs' (n: lib.nameValuePair "devShell-${n}") self'.devShells;
        in
        nixosMachines
        // packages
        // devShells
        // lib.mkIf (pkgs.stdenv.hostPlatform.isLinux) {
          master = import ./master.nix checkArgs;
          worker = import ./worker.nix checkArgs;
          effects = import ./effects.nix checkArgs;
        };
    };
}
