{ self, ... }:
{
  perSystem =
    {
      config,
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
          packages = lib.mapAttrs' (n: lib.nameValuePair "package-${n}") config.packages;
          devShells = lib.mapAttrs' (n: lib.nameValuePair "devShell-${n}") config.devShells;
        in
        nixosMachines
        // packages
        // devShells
        // {
          docsUpToDate = import ./docs.nix checkArgs;
        }
        // lib.optionalAttrs (pkgs.stdenv.hostPlatform.isLinux) {
          master = import ./master.nix checkArgs;
          worker = import ./worker.nix checkArgs;
          effects = import ./effects.nix checkArgs;
          poller = import ./poller.nix checkArgs;
          gitea = import ./gitea.nix checkArgs;
          scheduled-effects = import ./scheduled-effects.nix checkArgs;
        };
    };
}
