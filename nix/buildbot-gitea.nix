{ buildPythonPackage
, fetchPypi
, lib

, pip
, buildbot
, requests
}:
buildPythonPackage (lib.fix (self: {
  pname = "buildbot-gitea";
  version = "1.8.0";

  nativeBuildInputs = [

  ];

  propagatedBuildInputs = [
    pip
    buildbot
    requests
  ];

  src = fetchPypi {
    inherit (self) pname version;
    hash = "sha256-zYcILPp42QuQyfEIzmYKV9vWf47sBAQI8FOKJlZ60yA=";
  };
}))
