{ setuptools, buildPythonPackage }:
buildPythonPackage {
  name = "buildbot-nix";
  format = "pyproject";
  src = ./.;
  nativeBuildInputs = [ setuptools ];
}
