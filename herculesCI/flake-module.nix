{
  config,
  withSystem,
  ...
}:
{
  herculesCI = herculesCI: {
    onPush.default.outputs.effects.deploy = withSystem config.defaultEffectSystem (
      { pkgs, hci-effects, ... }:
      hci-effects.runIf (herculesCI.config.repo.branch == "main") (
        hci-effects.mkEffect {
          effectScript = ''
            echo "${builtins.toJSON { inherit (herculesCI.config.repo) branch tag rev; }}"
            ${pkgs.hello}/bin/hello
          '';
        }
      )
    );
  };
}
