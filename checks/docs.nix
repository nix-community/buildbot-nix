{ self, pkgs }:
pkgs.runCommand "docs-up-to-date" { } ''
  if ! diff --unified --ignore-blank-lines ${
    self.packages.${pkgs.stdenv.hostPlatform.system}.optionsDoc
  } ${../docs/OPTIONS.md} ; then
    exit 1
  else
    touch $out
  fi

''
