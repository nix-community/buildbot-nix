{
  buildPythonPackage,
  fetchPypi,
  lib,

  pip,
  buildbot,
  requests,
}:
buildPythonPackage (
  lib.fix (self: {
    pname = "buildbot-gitea";
    version = "1.8.0";

    propagatedBuildInputs = [
      pip
      buildbot
      requests
    ];

    patches = [ ./0001-reporter-create-status-in-the-base-repository-of-a-p.patch ];

    src = fetchPypi {
      inherit (self) pname version;
      hash = "sha256-zYcILPp42QuQyfEIzmYKV9vWf47sBAQI8FOKJlZ60yA=";
    };
  })
)
