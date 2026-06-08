# Gitea Integration

Buildbot-nix integrates with Gitea using access tokens for repository management
and OAuth2 for user authentication. This enables automatic webhook setup, commit
status updates, and secure authentication.

## Step 1: Create a Gitea Access Token

1. **Create a dedicated Gitea user** (recommended for organizations):
   - This user will manage webhooks and report build statuses
   - Add this user as a collaborator with **Administrator** permission to every
     repository you want to build: Gitea only allows repo admins to manage
     webhooks. Without admin the repository is still discovered, but the webhook
     (push, pull_request, pull_request_sync events) must be created manually.
     Private repositories are invisible to the token until the user is added.

2. **Generate an access token**:
   - Log in as the dedicated user
   - Go to Settings → Applications → Generate New Token
   - Required permissions:
     - `write:repository` - To create webhooks and update commit statuses
     - `read:user` - To list the repositories the user has access to
   - Save the token securely

## Step 2: Set up OAuth2 Authentication (for user login)

1. **Create an OAuth2 Application**:
   - Navigate to one of these locations:
     - Site Administration → Applications (for admins, applies globally)
     - Organization Settings → Applications (for organization-wide access)
     - User Settings → Applications (for personal use)

2. **Configure the OAuth2 app**:
   - **Application Name**: `nixbot`
   - **Redirect URI**: `https://nixbot.<your-domain>/auth/gitea/callback`

3. **Note the credentials**:
   - Client ID
   - Client Secret

## Step 3: Configure nixbot

Add the Gitea configuration to your NixOS module:

```nix
services.nixbot = {
  gitea = {
    enable = true;
    instanceUrl = "https://gitea.example.com";

    # Access token for API operations
    tokenFile = "/path/to/gitea-token";

    # OAuth2 for user authentication
    oauthId = "<oauth-client-id>";
    oauthSecretFile = "/path/to/oauth-secret";

    # Optional: SSH authentication for private repositories
    sshPrivateKeyFile = "/path/to/ssh-key";
    sshKnownHostsFile = "/path/to/known-hosts";

    # Optional: only allow these owners/repositories to be built
    userAllowlist = [ "my-org" ];
    repoAllowlist = [ "other-org/repo" ];

    # One-shot import: repositories with this topic are enabled on first
    # startup with an empty database; afterwards manage projects in the web UI
    topic = "build-with-buildbot";
  };
};
```

If webhooks must reach nixbot under a different URL than the web UI, set
`services.nixbot.webhookBaseUrl`.

## Step 4: Repository Configuration

For each repository you want to build:

1. **Grant repository access**:
   - Add the nixbot user as a collaborator with admin access
   - Admin access is required for webhook creation

2. **Enable the project**:
   - Toggle the project on in the web UI (as admin)

3. **Automatic webhook creation**:
   - Webhooks are created for enabled projects on every discovery cycle
     (startup, periodic refresh, manual reload) at
     `https://nixbot.<your-domain>/webhooks/gitea`
   - Each repository gets an auto-generated secret stored in the database;
     existing hooks are re-synced in place, leftover buildbot-era hooks pointing
     at this instance are removed
   - Webhook events: `push`, `pull_request` and `pull_request_sync`

4. **Manual webhook creation** (only when the nixbot user is not a repo admin;
   watch for the "no admin permission to manage webhooks" warning):

   1. Enable the project
   2. As a nixbot admin, open the repository page in the nixbot web UI, expand
      **webhook setup** and press **regenerate** - the secret is shown exactly
      once (rotating later invalidates the old secret; auto-managed hooks pick
      the new one up on the next discovery cycle)
   3. In the Gitea repository: Settings → Webhooks → Add Webhook → Gitea
   4. Target URL and Secret from step 2, POST Content Type `application/json`
   5. Trigger On: Custom Events → check **Push**, **Pull Request**, and **Pull
      Request Synchronized**
   6. Activate and save

## How It Works

- **Authentication**: Uses Gitea access tokens for API operations
- **Project Discovery**: Automatically discovers repositories where the nixbot
  user has admin access (restricted by `userAllowlist`/`repoAllowlist` if set);
  discovered projects are built once enabled in the web UI
- **Webhook Management**: Automatically creates and manages webhooks (push and
  pull request events) for enabled projects
- **Status Updates**: Reports build status back to Gitea commits and pull
  requests
- **Access Control**:
  - Admins: Configured users can reload projects and manage builds
  - Organization members: Can restart their own builds (when OAuth is
    configured)
- **Repository Access**: Can use either HTTPS (with token) or SSH authentication
  for cloning private repositories

## Troubleshooting

- **Projects not appearing**: Check that:
  - The nixbot user has admin access to the repository
  - The repository is not excluded by `userAllowlist`/`repoAllowlist`
  - The access token has the correct permissions
  - Reload projects manually through the web UI

- **Project appears but nothing builds**: Enable the project in the web UI

- **Webhooks not created**: Verify the project is enabled and the nixbot user
  has admin permissions on the repository

- **Authentication issues**:
  - Ensure the access token is valid and has required permissions
  - For OAuth issues, verify the redirect URI matches exactly

- **Private repositories**: If using SSH, ensure the SSH key is properly
  configured and the known_hosts file contains the Gitea server
