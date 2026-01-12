{
  buildPythonPackage,
  fetchFromGitHub,
  lib,
  pip,
  buildbot,
  requests,
}:
buildPythonPackage {
  pname = "buildbot-gitea";
  version = "1.8.0";
  format = "setuptools";

  src = fetchFromGitHub {
    owner = "Mic92";
    repo = "buildbot-gitea";
    rev = "f6f40088ba74b6a215aa7bb619cf79ed3cf36dfd";
    hash = "sha256-nYgVPgted37J3+SYiJc023bdP7jGEfkk4qdRDre+i8U=";
  };

  propagatedBuildInputs = [
    pip
    buildbot
    requests
  ];

  meta = with lib; {
    description = "Gitea plugin for Buildbot";
    homepage = "https://github.com/Mic92/buildbot-gitea";
    license = licenses.mit;
    maintainers = with maintainers; [ mic92 ];
  };
}
