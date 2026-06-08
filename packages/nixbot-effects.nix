{
  lib,
  bubblewrap,
  hatchling,
  buildPythonApplication,
}:
buildPythonApplication {
  name = "nixbot-effects";
  pyproject = true;
  src = ./../nixbot_effects;
  build-system = [
    hatchling
  ];
  makeWrapperArgs = [ "--prefix PATH : ${lib.makeBinPath [ bubblewrap ]}" ];
}
