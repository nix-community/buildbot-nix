{ pkgs, runCommand, nixosOptionsDoc, lib, self, ... }:
let
  eval = lib.evalModules {
    modules = [
      self.nixosModules."buildbot-master"
      self.nixosModules."buildbot-worker"
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

  optionsDoc = nixosOptionsDoc {
    inherit (eval) options;

    transformOptions =
      opt:
      opt
      // {
        # Clean up declaration sites to not refer to the buildbot-nix
        # source tree.
        declarations = map (
          decl:
          if lib.hasPrefix self.outPath (toString decl) then
            gitHubDeclaration "nix-community" "buildbot-nix" (
              lib.removePrefix "/" (lib.removePrefix self.outPath (toString decl))
            )
          else
            decl
        ) opt.declarations;
      };
  };
in
runCommand "options-doc.md" { } ''
  cat ${optionsDoc.optionsCommonMark} >> $out
''
