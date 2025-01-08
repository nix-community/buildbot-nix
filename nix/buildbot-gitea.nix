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

  src = fetchFromGitHub {
    owner = "Mic92";
    repo = "buildbot-gitea";
    rev = "a8e06d38f6654421aab787da04128756ce04a3df";
    hash = "sha256-z0Mj/PmTghziXJ6dV6qYFGZUuV0abMxzU+miqohDazU=";
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
