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
    rev = "3f3c9591384cd98ea5186b4375cdf142b9083737";
    hash = "sha256-bdjGijer/JSMTkBwhbeCx9pstFXFnRy0ZdC7mGaRX84=";
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
