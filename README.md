# Buildbot-nix

Buildbot-nix is a NixOS module designed to integrate
[Buildbot](https://www.buildbot.net/), a continuous integration (CI) framework,
into the Nix ecosystem. This module is under active development, and while it's
generally stable and widely used, please be aware that some APIs may change over
time.

## Features

- Fast, Parallel evaluation using
  [nix-eval-jobs](https://github.com/nix-community/nix-eval-jobs)
- Github integration:
  - Login with GitHub to control builds
  - CI status notification in pull requests and on the default branch
- All builds share the same nix store for speed
- The last attribute of a build is protected from garbage collection
- Build matrix based on `.#checks` attributes
- No arbitrary code runs outside of the Nix sandbox
- _experimental_
  [hercules-ci effect](https://docs.hercules-ci.com/hercules-ci-effects/) to run
  impure CI steps i.e. deploying NixOS

## Getting Started with Buildbot Setup

To set up Buildbot using Buildbot-nix, you can start by exploring the provided
examples:

- Check out the basic setup in [example](./examples/default.nix).
- Learn about configuring the Buildbot master in
  [master module](./nix/master.nix).
- Understand how to set up a Buildbot worker in
  [worker module](./nix/worker.nix).
- For local development, see
  [Local Development Guide](./docs/LOCAL_DEVELOPMENT.md).

Additionally, you can find real-world examples at the end of this document.

Buildbot masters and workers can be deployed either on the same machine or on
separate machines. To support multiple architectures, configure them as
[nix remote builders](https://nixos.org/manual/nix/stable/advanced-topics/distributed-builds).
For a practical NixOS example, see
[this remote builder configuration](https://github.com/Mic92/dotfiles/blob/main/machines/eve/modules/remote-builder.nix).

## Using `buildbot` with NixOS 24.05 (stable release)

The module applies custom patches that only apply to buildbot > 4.0.0. To use
buildbot-nix with NixOS 24.05, you should therefore not override the nixpkgs
input to your own stable version of buildbot-nix and leave it to the default
instead that is set to nixos-unstable-small.

So instead of using this in your flake

```
inputs = {
  buildbot-nix.url = "github:nix-community/buildbot-nix";
  buildbot-nix.inputs.nixpkgs.follows = "nixpkgs";
};
```

Just use:

```
inputs = {
  buildbot-nix.url = "github:nix-community/buildbot-nix";
};
```

An alternative is to point nixpkgs to your own version of nixpkgs-unstable in
case you are already using it elsewhere.

## Using Buildbot in Your Project

Buildbot-nix automatically triggers builds for your project under these
conditions:

- When a pull request is opened.
- When a commit is pushed to the default git branch.

It does this by evaluating the `.#checks` attribute of your project's flake in
parallel. Each attribute found results in a separate build step. You can test
these builds locally using `nix flake check -L` or
[nix-fast-build](https://github.com/Mic92/nix-fast-build).

If you need to build other parts of your flake, such as packages or NixOS
machines, you should re-export these into the `.#checks` output. Here are two
examples to guide you:

- Using
  [flake-parts](https://github.com/Mic92/dotfiles/blob/10890601a02f843b49fe686d7bc19cb66a04e3d7/flake.nix#L139).
- A
  [plain flake example](https://github.com/nix-community/nixos-images/blob/56b52791312edeade1e6bd853ce56c778f363d50/flake.nix#L53).

### Authentication backend

At the moment `buildbot-nix` offers two access modes, `public` and
`fullyPrivate`. `public` is the default and gives read-only access to all of
buildbot, including builds, logs and builders. For read-write access,
authentication is still needed, this is controlled by the `authBackend` option.

`fullyPrivate` will hide buildbot behind `oauth2-proxy` which protects the whole
buildbot instance. buildbot fetches the currently authenticated user from
`oauth2-proxy` so the same admin, organisation rules apply.

`fullyPrivate` acccess mode is a workaround as buildbot does not support hiding
information natively as now.

#### Public

For some actions a login is required. The authentication backend is set by the
`services.buildbot-nix.master.authBackend` NixOS option ("github", "gitea", or
others).

**Note**: You can configure both GitHub and Gitea integrations simultaneously,
regardless of which authentication backend you choose. The auth backend only
determines how users log in to the Buildbot interface.

We have the following two roles:

- Admins:
  - The list of admin usernames is hard-coded in the NixOS configuration.
  - admins can reload the project list
- Organisation member:
  - All member of the organisation where this repository is located
  - They can restart builds

##### Integration with GitHub

Buildbot-nix uses GitHub App authentication to integrate with GitHub
repositories. This enables automatic webhook setup, commit status updates, and
secure authentication.

###### Step 1: Create a GitHub App

1. Navigate to:
   - For personal accounts: `https://github.com/settings/apps/new`
   - For organizations:
     `https://github.com/organizations/<org>/settings/apps/new`

2. Configure the app with these settings:
   - **GitHub App Name**: `buildbot-nix-<org>` (or any unique name)
   - **Homepage URL**: `https://buildbot.<your-domain>`
   - **Webhook**: Disable (buildbot-nix creates webhooks per repository)
   - **Callback URL** (optional, for OAuth):
     `https://buildbot.<your-domain>/auth/login`

3. Set the required permissions:
   - **Repository Permissions:**
     - Contents: Read-only (to clone repositories)
     - Commit statuses: Read and write (to report build status)
     - Metadata: Read-only (basic repository info)
     - Webhooks: Read and write (to automatically create webhooks)
   - **Organization Permissions** (if app is for an organization):
     - Members: Read-only (to verify organization membership for access control)

4. After creating the app:
   - Note the **App ID**
   - Generate and download a **private key** (.pem file)

###### Step 2: Configure buildbot-nix

Add the GitHub configuration to your NixOS module:

```nix
services.buildbot-nix.master = {
  authBackend = "github";
  github = {
    appId = <your-app-id>;  # The numeric App ID
    appSecretKeyFile = "/path/to/private-key.pem";  # Path to the downloaded private key

    # Optional: Enable OAuth for user login
    oauthId = "<oauth-client-id>";
    oauthSecretFile = "/path/to/oauth-secret";

    # Optional: Filter which repositories to build
    topic = "buildbot-nix";  # Only build repos with this topic
  };
};
```

###### Step 3: Install the GitHub App

1. Go to your app's settings page
2. Click "Install App" and choose which repositories to grant access
3. The app needs access to all repositories you want to build with buildbot-nix

###### Step 4: Repository Configuration

For each repository you want to build:

1. **Add the configured topic** (if using topic filtering):
   - Go to repository settings
   - Add the topic (e.g., `buildbot-nix`) to enable builds

2. **Automatic webhook creation**:
   - Buildbot-nix automatically creates webhooks when:
     - Projects are loaded on startup
     - The project list is manually reloaded
   - The webhook will be created at:
     `https://buildbot.<your-domain>/change_hook/github`

###### How It Works

- **Authentication**: Uses GitHub App JWT tokens for API access and installation
  tokens for repository-specific operations
- **Project Discovery**: Automatically discovers repositories the app has access
  to, filtered by topic if configured
- **Webhook Management**: Automatically creates and manages webhooks for push
  and pull_request events
- **Status Updates**: Reports build status back to GitHub commits and pull
  requests
- **Access Control**:
  - Admins: Configured users can reload projects and manage builds
  - Organization members: Can restart their own builds

###### Troubleshooting

- **Projects not appearing**: Check that:
  - The GitHub App is installed for the repository
  - The repository has the configured topic (if filtering by topic)
  - Reload projects manually through the Buildbot UI

- **Webhooks not created**: Verify the app has webhook write permission for the
  repository

- **Authentication issues**: Ensure the private key file is readable by the
  buildbot service

##### Integration with Gitea

Buildbot-nix integrates with Gitea using access tokens for repository management
and OAuth2 for user authentication. This enables automatic webhook setup, commit
status updates, and secure authentication.

###### Step 1: Create a Gitea Access Token

1. **Create a dedicated Gitea user** (recommended for organizations):
   - This user will manage webhooks and report build statuses
   - Add this user as a collaborator to all repositories you want to build

2. **Generate an access token**:
   - Log in as the dedicated user
   - Go to Settings → Applications → Generate New Token
   - Required permissions:
     - `write:repository` - To create webhooks and update commit statuses
     - `write:user` - To access user information
   - Save the token securely

###### Step 2: Set up OAuth2 Authentication (for user login)

1. **Create an OAuth2 Application**:
   - Navigate to one of these locations:
     - Site Administration → Applications (for admins, applies globally)
     - Organization Settings → Applications (for organization-wide access)
     - User Settings → Applications (for personal use)

2. **Configure the OAuth2 app**:
   - **Application Name**: `buildbot-nix`
   - **Redirect URI**: `https://buildbot.<your-domain>/auth/login`

3. **Note the credentials**:
   - Client ID
   - Client Secret

###### Step 3: Configure buildbot-nix

Add the Gitea configuration to your NixOS module:

```nix
services.buildbot-nix.master = {
  authBackend = "gitea";
  gitea = {
    enable = true;
    instanceUrl = "https://gitea.example.com";

    # Access token for API operations
    tokenFile = "/path/to/gitea-token";
    webhookSecretFile = "/path/to/webhook-secret";

    # OAuth2 for user authentication
    oauthId = "<oauth-client-id>";
    oauthSecretFile = "/path/to/oauth-secret";

    # Optional: SSH authentication for private repositories
    sshPrivateKeyFile = "/path/to/ssh-key";
    sshKnownHostsFile = "/path/to/known-hosts";

    # Optional: Filter which repositories to build
    topic = "buildbot-nix";  # Only build repos with this topic
  };
};
```

###### Step 4: Repository Configuration

For each repository you want to build:

1. **Grant repository access**:
   - Add the buildbot user as a collaborator with admin access
   - Admin access is required for webhook creation

2. **Add the configured topic** (if using topic filtering):
   - Go to repository settings
   - Add the topic (e.g., `buildbot-nix`) to enable builds

3. **Automatic webhook creation**:
   - Buildbot-nix automatically creates webhooks when:
     - Projects are loaded on startup
     - The project list is manually reloaded
   - The webhook will be created at:
     `https://buildbot.<your-domain>/change_hook/gitea`
   - Webhook events: `push` and `pull_request`

###### How It Works

- **Authentication**: Uses Gitea access tokens for API operations
- **Project Discovery**: Automatically discovers repositories where the buildbot
  user has admin access, filtered by topic if configured
- **Webhook Management**: Automatically creates and manages webhooks for push
  and pull_request events
- **Status Updates**: Reports build status back to Gitea commits and pull
  requests
- **Access Control**:
  - Admins: Configured users can reload projects and manage builds
  - Organization members: Can restart their own builds (when OAuth is
    configured)
- **Repository Access**: Can use either HTTPS (with token) or SSH authentication
  for cloning private repositories

###### Troubleshooting

- **Projects not appearing**: Check that:
  - The buildbot user has admin access to the repository
  - The repository has the configured topic (if filtering by topic)
  - The access token has the correct permissions
  - Reload projects manually through the Buildbot UI

- **Webhooks not created**: Verify the buildbot user has admin permissions on
  the repository

- **Authentication issues**:
  - Ensure the access token is valid and has required permissions
  - For OAuth issues, verify the redirect URI matches exactly

- **Private repositories**: If using SSH, ensure the SSH key is properly
  configured and the known_hosts file contains the Gitea server

#### Fully Private

To enable fully private mode, set `acessMode.fullyPrivate` to an attrset
containing the required options for fully private use, refer to the examples and
module implementation (`nix/master.nix`).

This access mode honors the `admins` option in addition to the
`accessMode.fullyPrivate.organisations` option. To allow access from certain
organisations, you must explicitly list them.

If you've set `authBackend` previously, unset it, or you will get an error about
a conflicting definitions. `fullyPrivate` requires the `authBackend` to be set
to `basichttpauth` to function (this is handled by the module, which is why you
can leave it unset). For a concrete example please refer to
[fully-private-github](./examples/fully-private-github.nix)

### Per Repository Configuration

Currently `buildbot-nix` will look for a file named `buildbot-nix.toml` in the
root of whichever branch it's currently evaluating, parse it as TOML and apply
the configuration specified. The following table illustrates the supported
options.

|           | key         | type  | description                                                                 | default      | example                                                                                                                                                                                                 |
| :-------- | :---------- | :---- | :-------------------------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| lock file | `lock_file` | `str` | dictates which lock file `buildbot-nix` will use when evaluating your flake | `flake.lock` | have multiple lockfiles, one for `nixpkgs-stable`, one for `nixpkgs-unstable` or by default pin an input to a private repo, but have a lockfile with that private repo replaced by a public repo for CI |
| attribute | `attribute` | `str` | which attribute in the flake to evaluate and build                          | `checks`     | using a different attribute, like `hydraJobs`                                                                                                                                                           |
| flake_dir | `flake_dir` | `str` | which directory the flake is located                                        | `.`          | using a different flake, like `./tests`                                                                                                                                                                 |

## Binary caches

To access the build results on other machines there are two options at the
moment

#### Local binary cache (harmonia)

You can set up a binary cache on your buildbot-worker machine to make its nix
store accessible from other machines. Check out the README of the
[project](https://github.com/nix-community/harmonia/?tab=readme-ov-file#configuration-for-public-binary-cache-on-nixos),
for an example configuration

#### Cachix

Buildbot-nix also supports pushing packages to cachix. Check out the comment out
[example configuration](https://github.com/Mic92/buildbot-nix/blob/main/examples/master.nix)
in our repository.

#### Attic

Buildbot-nix does not have native support for pushing packages to
[attic](https://github.com/zhaofengli/attic) yet. However it's possible to
integrate run a systemd service as described in
[this example configuration](./examples/attic-watch-store.nix). The systemd
service watches for changes in the local buildbot-nix store and uploads the
contents to the attic cache.

## (experimental) Hercules CI effects

See [flake.nix](flake.nix) for an example and
[https://docs.hercules-ci.com/hercules-ci/effects/] for documentation.

You can run hercules-effects locally using the `buildbot-effects` cli:

```
$ buildbot-effects --help
usage: buildbot-effects [-h] [--secrets SECRETS] [--rev REV] [--branch BRANCH] [--repo REPO] [--path PATH] {list,run,run-all} ...

Run effects from a hercules-ci flake

positional arguments:
  {list,run,run-all}  Command to run
    list              List available effects
    run               Run an effect
    run-all           Run all effects

options:
  -h, --help          show this help message and exit
  --secrets SECRETS   Path to a json file with secrets
  --rev REV           Git revision to use
  --branch BRANCH     Git branch to use
  --repo REPO         Git repo to prepend to be
  --path PATH         Path to the repository
```

Example from the buildbot-nix repository:

```console
$ git clone github.com/nix-community/buildbot-nix
$ cd buildbot-nix
```

```console
$ nix run github:nix-community/buildbot-nix#buildbot-effects -- list
[
  "deploy"
]
```

```console
$ nix run github:nix-community/buildbot-nix#buildbot-effects -- --branch main run deploy
{branch:main,rev:5d2e0af1cfccfc209b893b89392cf80f5640d936,tag:null}
Hello, world!
```

## Incompatibilities with the lix overlay

The lix overlay overrides nix-eval-jobs with a version that doesn't work with
buildbot-nix because of missing features and therefore cannot be used together
with the buildbot-nix module.

Possible workaround: Don't use the overlay and only set the
`nix.package = pkgs.lix;` NixOS option.

## Real-World Deployments

See Buildbot-nix in action in these deployments:

The following instances run on GitHub:

- [**Nix-community infra**](https://nix-community.org/):
  [Configuration](https://github.com/nix-community/infra/tree/master/modules/nixos)
  | [Instance](https://buildbot.nix-community.org/)
- [**Mic92's dotfiles**](https://github.com/Mic92/dotfiles):
  [Configuration](https://github.com/Mic92/dotfiles/blob/main/nixos/eve/modules/buildbot.nix)
  | [Instance](https://buildbot.thalheim.io/)
- [**Technical University Munich**](https://dse.in.tum.de/):
  [Configuration](https://github.com/TUM-DSE/doctor-cluster-config/tree/master/modules/buildbot)
  | [Instance](https://buildbot.dse.in.tum.de/)
- [**Numtide**](https://numtide.com/): [Instance](https://buildbot.numtide.com)
- [**Ngi0**](https://www.ngi.eu/ngi-projects/ngi-zero/):
  [Instance](https://buildbot.ngi.nixos.org/#/projects)

The following instances integrated with Gitea:

- **Clan infra**:
  [Configuration](https://git.clan.lol/clan/clan-infra/src/branch/main/modules/buildbot.nix)
  | [Instance](https://buildbot.clan.lol/)

## Get in touch

We have a matrix channel at
[buildbot-nix](https://matrix.to/#/#buildbot-nix:thalheim.io).

## Need commercial support or customization?

For commercial support, please contact [Mic92](https://github.com/Mic92/) at
joerg@thalheim.io or reach out to [Numtide](https://numtide.com/contact/).
