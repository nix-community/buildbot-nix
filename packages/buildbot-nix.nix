{
  buildPythonPackage,
  git,
  hatchling,
  nix,
  pydantic,
  pytestCheckHook,
  pytest-timeout,
  pytest-xdist,
  pytest-benchmark,
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
buildPythonPackage (finalAttrs: {
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

  # Tests run in passthru.tests.pytest to keep the test closure
  # (playwright browsers, postgresql) out of the package build.
  doCheck = false;

  passthru.tests.pytest = finalAttrs.finalPackage.overrideAttrs {
    name = "buildbot-nix-tests";
    doCheck = true;
  };

  nativeCheckInputs = [
    git
    pytestCheckHook
    pytest-timeout
    pytest-xdist
    pytest-benchmark
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
    description = "A standalone CI service for Nix projects";
    homepage = "https://github.com/nix-community/buildbot-nix";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.mic92 ];
  };
})
