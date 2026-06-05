{
  buildPythonPackage,
  git,
  hatchling,
  nix,
  pydantic,
  pytestCheckHook,
  pytest-timeout,
  pytest-xdist,
  fastapi,
  uvicorn,
  asyncpg,
  jinja2,
  httpx,
  zstandard,
  python-multipart,
  postgresql,
  playwright,
  playwright-driver,
  makeFontsConf,
  dejavu_fonts,
  lib,
}:
buildPythonPackage {
  name = "buildbot-nix";
  pyproject = true;
  src = ./../buildbot_nix;
  build-system = [ hatchling ];
  dependencies = [
    pydantic
    fastapi
    uvicorn
    asyncpg
    jinja2
    httpx
    zstandard
    python-multipart
  ];

  buildInputs = [ nix ];

  nativeCheckInputs = [
    git
    pytestCheckHook
    pytest-timeout
    pytest-xdist
    postgresql
    playwright
  ];

  # Browser tests run headless Chromium from the pinned playwright
  # driver; the version matches the python playwright package.
  # Chromium refuses to start with the unwritable default HOME.
  preCheck = ''
    export HOME=$(mktemp -d)
  '';

  env = {
    PLAYWRIGHT_BROWSERS_PATH = playwright-driver.browsers;
    PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
    # Chromium aborts in Skia without a fontconfig setup.
    FONTCONFIG_FILE = makeFontsConf { fontDirectories = [ dejavu_fonts ]; };
  };

  meta = {
    description = "A standalone CI engine for Nix projects";
    homepage = "https://github.com/nix-community/buildbot-nix";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.mic92 ];
  };
}
