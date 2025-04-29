{
  lib,
  bubblewrap,
  setuptools,
  buildPythonApplication,
}:
buildPythonApplication {
  name = "buildbot-effects";
  pyproject = true;
  src = ./../buildbot_effects;
  build-system = [
    setuptools
  ];
  makeWrapperArgs = [ "--prefix PATH : ${lib.makeBinPath [ bubblewrap ]}" ];
}
