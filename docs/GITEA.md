# Gitea Integration

Buildbot-nix integrates with Gitea using access tokens for repository management
and OAuth2 for user authentication. This enables automatic webhook setup, commit
status updates, and secure authentication.

## Step 1: Create a Gitea Access Token

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

## Step 2: Set up OAuth2 Authentication (for user login)

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

## Step 3: Configure buildbot-nix

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

## Step 4: Repository Configuration

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

## How It Works

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

## Troubleshooting

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
