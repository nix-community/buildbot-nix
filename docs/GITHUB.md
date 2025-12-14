# GitHub Integration

Buildbot-nix uses GitHub App authentication to integrate with GitHub
repositories. This enables automatic webhook setup, commit status updates, and
secure authentication.

## Step 1: Create a GitHub App

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

## Step 2: Configure buildbot-nix

Add the GitHub configuration to your NixOS module:

```nix
services.buildbot-nix.master = {
  enable = true;
  domain = "buildbot.example.com";  # Your buildbot domain
  workersFile = "/path/to/workers.json";  # See workers file format below
  authBackend = "github";
  github = {
    appId = <your-app-id>;  # The numeric App ID
    appSecretKeyFile = "/path/to/private-key.pem";  # Path to the downloaded private key

    # Required for GitHub authBackend: Enable OAuth for user login
    oauthId = "<oauth-client-id>";
    oauthSecretFile = "/path/to/oauth-secret";

    # A random secret used to verify incoming webhooks from GitHub
    webhookSecretFile = "/path/to/webhook-secret";

    # Optional: Filter which repositories to build
    topic = "buildbot-nix";  # Only build repos with this topic
  };
};
```

### Workers File Format

The `workersFile` option expects a JSON file containing an array of worker
definitions:

```json
[
  { "name": "worker1", "pass": "secret-password", "cores": 16 }
]
```

- `name`: The worker name (matches `services.buildbot-nix.worker.name`, defaults
  to hostname)
- `pass`: The password (must match
  `services.buildbot-nix.worker.workerPasswordFile` contents)
- `cores`: Number of CPU cores (must match the actual core count of the worker
  machine)

## Step 3: Install the GitHub App

1. Go to your app's settings page
2. Click "Install App" and choose which repositories to grant access
3. The app needs access to all repositories you want to build with buildbot-nix

## Step 4: Repository Configuration

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

## How It Works

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

## Troubleshooting

- **Projects not appearing**: Check that:
  - The GitHub App is installed for the repository
  - The repository has the configured topic (if filtering by topic)
  - Reload projects manually through the Buildbot UI

- **Webhooks not created**: Verify the app has webhook write permission for the
  repository

- **Authentication issues**: Ensure the private key file is readable by the
  buildbot service
