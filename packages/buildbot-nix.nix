{
  buildPythonPackage,
  buildbot-gitea,
  nix,
  psycopg2,
  pydantic,
  pytest,
  pytestCheckHook,
  requests,
  setuptools,
  treq,
  lib,
}:
buildPythonPackage {
  name = "buildbot-nix";
  pyproject = true;
  src = ./../buildbot_nix;
  build-system = [ setuptools ];
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
