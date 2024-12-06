{
  setuptools,
  buildPythonPackage,
  pydantic,
  requests,
  treq,
  psycopg2,
  nix,
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
  ];

  buildInputs = [ nix ];
}
