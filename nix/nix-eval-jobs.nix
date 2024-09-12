{
  fetchFromGitHub,
  nix-eval-jobs,
  nixVersions,
  boost,
  curl,
  nlohmann_json,
}:
nix-eval-jobs.overrideAttrs (oldAttrs: {
  src = fetchFromGitHub {
    owner = "nix-community";
    repo = "nix-eval-jobs";
    # https://github.com/nix-community/nix-eval-jobs/pull/325
    rev = "91ca6cffaecbe5d0df79d2d97d4b286252c17aef";
    sha256 = "sha256-uTroApEsrSVxGtKcnrOBSEGImS3UolxMmy/9z97FpWE=";
  };

  buildInputs = [
    boost
    nixVersions.nix_2_24
    curl
    nlohmann_json
  ];
})
