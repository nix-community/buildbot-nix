{
  buildPythonPackage,
  buildbot-gitea,
  hatchling,
  nix,
  psycopg2,
  pydantic,
  pytestCheckHook,
  requests,
  treq,
  lib,
}:
buildPythonPackage {
  name = "buildbot-nix";
  pyproject = true;
  src = ./../buildbot_nix;
  build-system = [ hatchling ];
  dependencies = [
    pydantic
    requests
    treq
    psycopg2
    buildbot-gitea
  ];

  buildInputs = [ nix ];

  nativeCheckInputs = [
    pytestCheckHook
  ];

  meta = {
    description = "Buildbot plugin for building Nix projects";
    homepage = "https://github.com/nix-community/buildbot-nix";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.mic92 ];
  };
}
