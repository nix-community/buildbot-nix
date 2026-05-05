# Minimal subset of hercules-ci-effects sufficient for buildbot-effects.
# Avoids pulling hercules-ci-effects (and its transitive flake-parts input)
# into every consumer of this flake.
{ pkgs }:
{
  mkEffect =
    {
      effectScript ? "",
      userSetupScript ? "",
      name ? "effect",
      inputs ? [ ],
      secretsMap ? { },
    }:
    pkgs.stdenvNoCC.mkDerivation {
      inherit name effectScript userSetupScript;
      isEffect = true;
      secretsMap = builtins.toJSON secretsMap;
      nativeBuildInputs = [
        pkgs.cacert
        pkgs.curl
        pkgs.jq
      ]
      ++ inputs;
      phases = [
        "initPhase"
        "userSetupPhase"
        "effectPhase"
      ];
      initPhase = ''
        exec </dev/null
        export HOME=/build/home
        mkdir -p "$HOME"
      '';
      userSetupPhase = ''eval "$userSetupScript"'';
      effectPhase = ''eval "$effectScript"'';
    };

  # When the condition is false we still want eval/build of the effect's
  # closure to succeed, so return a no-op effect instead.
  runIf =
    condition: effect:
    if condition then
      { run = effect; }
    else
      {
        dependencies = effect.inputDerivation // {
          isEffect = false;
          buildDependenciesOnly = true;
        };
      };
}
