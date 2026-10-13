# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

from urllib.parse import urlsplit

from sphinx.util import logging

logger = logging.getLogger(__name__)

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "buildbot-nix"
# copyright = '2026, buildbot-nix contributors'
# author = 'buildbot-nix contributors'

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinxcontrib_nixdomain",
    "sphinx_design",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]


# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "furo"
html_static_path = ["_static"]
html_css_files = ["field-lists.css"]


# -- Options for the MyST parser ---------------------------------------------
# https://myst-parser.readthedocs.io/en/latest/configuration.html

# These syntax extensions are required by `sphinxcontrib-nixdomain`
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
]

# -- Options for the Nix domain ----------------------------------------------
# https://sphinxcontrib-nixdomain.readthedocs.io/en/stable/reference/configuration.html


def nixdomain_linkcode_resolve(path: str) -> str:
    url = urlsplit(path)
    fragment = "#" + url.fragment if url.fragment else ""

    match url.netloc:
        case "self":
            return f"https://github.com/nix-community/buildbot-nix/blob/main{url.path}{fragment}"
        case "nixpkgs":
            return f"https://github.com/NixOS/nixpkgs/blob/master{url.path}{fragment}"

    logger.warning("no source repository for url: %s", path)
    return ""
