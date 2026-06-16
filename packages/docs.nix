{
  stdenvNoCC,
  lib,
  python,
  nixdomainObjects,
}:
stdenvNoCC.mkDerivation {
  name = "buildbot-nix-docs";

  src = ../.;
  setSourceRoot = "sourceRoot=$(echo */docs)";

  nativeBuildInputs = with python.pkgs; [
    furo
    myst-parser
    sphinx
    sphinx-design
    sphinxcontrib-nixdomain
  ];

  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    make html
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/doc/buildbot-nix/
    cp -r _build/html $out/share/doc/buildbot-nix/
    runHook postInstall
  '';

  env.NIXDOMAIN_OBJECTS = nixdomainObjects;

  meta = {
    description = "buildbot-nix documentation";
    license = lib.licenses.mit;
  };
}
