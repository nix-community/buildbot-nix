{
  lib,
  bubblewrap,
  hatchling,
  buildPythonApplication,
}:
buildPythonApplication {
  name = "buildbot-effects";
  pyproject = true;
  src = ./../buildbot_effects;
  build-system = [
    hatchling
  ];
  makeWrapperArgs = [ "--prefix PATH : ${lib.makeBinPath [ bubblewrap ]}" ];
}
