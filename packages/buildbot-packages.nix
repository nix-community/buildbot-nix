{
  pkgs,
  lib,
  newScope,
}:
let
  # Patch twisted to handle ENOENT in epoll reactor when fd is closed before registration.
  # This fixes HTTP 502 errors caused by a race condition where a connection is accepted
  # but reset by the peer before the reactor can register it with epoll.
  python3 = pkgs.python3.override {
    packageOverrides = final: prev: {
      twisted = prev.twisted.overrideAttrs (old: {
        patches = (old.patches or [ ]) ++ [ ../patches/twisted-epoll-enoent.patch ];
      });
    };
  };
in
lib.makeScope (self: newScope (self.python.pkgs // self)) (self: {
  python = python3;
  buildbot-pkg =
    self.callPackage "${pkgs.path}/pkgs/development/tools/continuous-integration/buildbot/pkg.nix"
      { };
  buildbot-worker =
    self.callPackage "${pkgs.path}/pkgs/development/tools/continuous-integration/buildbot/worker.nix"
      { };
  buildbot =
    self.callPackage "${pkgs.path}/pkgs/development/tools/continuous-integration/buildbot/master.nix"
      { };
  buildbot-plugins = lib.recurseIntoAttrs (
    self.callPackage "${pkgs.path}/pkgs/development/tools/continuous-integration/buildbot/plugins.nix"
      { }
  );
})
