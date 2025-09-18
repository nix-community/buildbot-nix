{
  buildPythonPackage,
  buildbot-gitea,
  nix,
  psycopg2,
  pydantic,
  requests,
  setuptools,
  treq,
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
}
