{
  fetchFromGitHub,
  nix-eval-jobs,
  nixVersions,
  boost,
  curl,
  nlohmann_json,
}:
nix-eval-jobs.overrideAttrs (_oldAttrs: {
  src = fetchFromGitHub {
    owner = "nix-community";
    repo = "nix-eval-jobs";
    # https://github.com/nix-community/nix-eval-jobs/pull/325
    rev = "e5a2c008b922c1a7642f93d29645403b20c70fec";
    sha256 = "sha256-UIY4EFvzsxYK8FhT6RSsmVDLqDBHDMzROy1g4YisIgY=";
  };

  buildInputs = [
    boost
    nixVersions.nix_2_24
    curl
    nlohmann_json
  ];
})
