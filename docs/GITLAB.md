# GitLab Integration

Buildbot-nix integrates with GitLab using an access token. GitLab has no
GitHub-App equivalent, so the setup follows the Gitea model: token-based API
access, per-repository webhooks, commit status updates.

## Step 1: Create a GitLab Access Token

Use one of:

- **Personal access token** (simplest; CI acts as your user): User Settings →
  Access tokens → Add new token, scope `api`
- **Group or project access token** (separate bot identity): Group/Project
  Settings → Access tokens, role **Maintainer**, scope `api`
- **Service account** (self-managed admin or paid tier)

Maintainer permission on a project is required for automatic webhook
registration; without it the project is still discovered and built, but the
webhook must be created manually (see below).

## Step 2: Configure nixbot

```nix
services.nixbot = {
  gitlab = {
    enable = true;
    # instanceUrl defaults to https://gitlab.com
    tokenFile = "/path/to/gitlab-token";

    # Optional: restrict which repositories are built
    userAllowlist = [ "mygroup" ];
    # repoAllowlist = [ "mygroup/myrepo" ];

    # Optional: SSH authentication for fetching
    # sshPrivateKeyFile = "/path/to/ssh-key";
    # sshKnownHostsFile = "/path/to/known-hosts";
  };
};
```

## Step 3: Enable Projects

1. Open the nixbot web UI as an admin
2. Enable the repository on the dashboard
3. With Maintainer permission the webhook (push + merge request events) is
   registered automatically on the next discovery cycle

## Manual webhook creation

Only needed when the token lacks Maintainer on the project (watch for the "no
maintainer permission to manage webhooks" warning):

1. Enable the project
2. On the repository page in the nixbot web UI, expand **webhook setup** and
   press **regenerate** - the secret is shown exactly once
3. In GitLab: Settings → Webhooks → Add new webhook
4. URL from step 2, Secret token from step 2
5. Trigger: **Push events** and **Merge request events**
6. Save

## Notes

- Commit statuses are posted per attribute plus `buildbot/nix-eval`; use them in
  merge request approval rules / merged results pipelines.
- GitLab does not sign webhook payloads; the secret is compared against the
  `X-Gitlab-Token` header. Use HTTPS for the webhook URL.
