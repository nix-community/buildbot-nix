{
  bash,
  buildbot,
  buildbot-effects,
  buildbot-gitea,
  buildbot-nix,
  buildbot-plugins,
  buildbot-worker,
  cachix,
  coreutils,
  git,
  lib,
  nix,
  nix-eval-jobs,
  openssh,
  python,
  writeShellScriptBin,
}:
let
  pythonEnv = python.withPackages (ps: [
    ps.twisted
    (ps.toPythonModule buildbot)
    (ps.toPythonModule buildbot-worker)
    buildbot-nix
    buildbot-gitea
    buildbot-effects
    buildbot-plugins.www
  ]);
in
writeShellScriptBin "buildbot-dev" ''
  set -xeuo pipefail
  git_root=$(git rev-parse --show-toplevel)
  export PATH=${
    lib.makeBinPath [
      nix-eval-jobs
      cachix
      git
      openssh
      nix
      bash
      coreutils
      buildbot-effects
    ]
  }
  mkdir -p "$git_root/.buildbot-dev"
  cd "$git_root/.buildbot-dev"
  "${pythonEnv}/bin/buildbot" create-master .
  #if [ ! -f master.cfg ]; then
  install -m600 ${./master.cfg.py} master.cfg
  #fi
  echo > $git_root/.buildbot-dev/twistd.log
  tail -f $git_root/.buildbot-dev/twistd.log &
  tail_pid=$!
  trap 'kill $tail_pid' EXIT
  "${pythonEnv}/bin/buildbot" start --nodaemon .
''
