{ pkgs }:
let
  buildbotPackages = pkgs.callPackage ./buildbot-packages.nix { };
in
buildbotPackages.buildbot
