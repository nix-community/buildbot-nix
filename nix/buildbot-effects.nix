{
  lib,
  python3,
  bubblewrap,
  setuptools,
  buildPythonApplication,
}:
buildPythonApplication {
  name = "buildbot-effects";
  format = "pyproject";
  src = ./..;
  nativeBuildInputs = [
    setuptools
  ];
  makeWrapperArgs = [ "--prefix PATH : ${lib.makeBinPath [ bubblewrap ]}" ];

  postFixup = ''
    rm -rf $out/${python3.pkgs.python.sitePackages}/buildbot_nix
  '';
}
