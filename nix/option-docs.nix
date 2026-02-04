{ inputs, ... }:
{
  perSystem =
    { pkgs, lib, ... }:
    {
      packages.optionsDoc =
        let
          eval = lib.evalModules {
            modules = [
              ./nixosModules/master.nix
              ./nixosModules/worker.nix
              {
                config._module.check = false;
                options._module.args = lib.mkOption {
                  internal = true;
                };
              }
            ];
            specialArgs = {
              inherit pkgs;
            };
          };

          gitHubDeclaration =
            user: repo: subpath:
            let
              urlRef = "main";
            in
            {
              url = "https://github.com/${user}/${repo}/blob/${urlRef}/${subpath}";
              name = "<${repo}/${subpath}>";
            };

          optionsDoc = pkgs.nixosOptionsDoc {
            inherit (eval) options;

            transformOptions =
              opt:
              opt
              // {
                # Clean up declaration sites to not refer to the Home Manager
                # source tree.
                declarations = map (
                  decl:
                  if lib.hasPrefix inputs.self.outPath (toString decl) then
                    gitHubDeclaration "nix-community" "buildbot-nix" (
                      lib.removePrefix "/" (lib.removePrefix inputs.self.outPath (toString decl))
                    )
                  else
                    decl
                ) opt.declarations;
              };
          };
        in
        pkgs.runCommand "options-doc.md" { } ''
          cat ${optionsDoc.optionsCommonMark} >> $out
        '';
    };
}
