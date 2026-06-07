# OIDC Authentication

buildbot-nix supports generic OpenID Connect (OIDC) authentication, allowing you
to use any OIDC-compliant identity provider (Keycloak, PocketID, Authentik,
etc.) for user login.

## NixOS Configuration

```nix
{
  services.buildbot-nix = {
    oidc.enable = true;

    # Provider-qualified identities: "oidc:<issuer-host>:<preferred_username>"
    admins = [
      "oidc:keycloak.example.com:alice"
      "oidc:keycloak.example.com:bob"
    ];

    oidc = {
      # Display name shown on login button
      name = "My Identity Provider";

      # OIDC discovery endpoint URL
      discoveryUrl = "https://keycloak.example.com/realms/myrealm/.well-known/openid-configuration";

      clientId = "buildbot";
      clientSecretFile = "/run/secrets/buildbot-oidc-secret";

      # Scopes to request (defaults shown)
      scope = [ "openid" "email" "profile" ];

      # Optional: include groups scope for group-based authorization
      # scope = [ "openid" "email" "profile" "groups" ];

      # Claim mapping (defaults shown)
      mapping = {
        email = "email";
        username = "preferred_username";  # Used for admin matching
        full_name = "name";
        groups = null;  # Set to "groups" if using group sync
      };
    };
  };
}
```

Set your OIDC provider's callback URL to:
`https://buildbot.example.com/auth/oidc/callback`

## Manual Configuration

For non-NixOS setups or local development, the service is configured via a JSON
file passed to `buildbot-nix --config`:

```json
{
  "admins": [
    "oidc:keycloak.example.com:alice",
    "oidc:keycloak.example.com:bob"
  ],
  "auth_backend": "oidc",
  "oidc": {
    "name": "My Identity Provider",
    "discovery_url": "https://keycloak.example.com/realms/myrealm/.well-known/openid-configuration",
    "client_id": "buildbot",
    "client_secret_file": "/path/to/client_secret",
    "scope": ["openid", "email", "profile"],
    "mapping": {
      "email": "email",
      "username": "preferred_username",
      "full_name": "name",
      "groups": null
    }
  }
}
```

## Provider Examples

### Keycloak

```nix
oidc = {
  name = "Keycloak";
  discoveryUrl = "https://keycloak.example.com/realms/{realm-name}/.well-known/openid-configuration";
  clientId = "buildbot";
  clientSecretFile = "/run/secrets/keycloak-secret";
};
```

### Authelia

See [examples/oidc-authelia.nix](../examples/oidc-authelia.nix) for a complete
configuration including the Authelia side: the client registration (Authelia
stores only a digest of the client secret) and the provider's HMAC secret and
issuer key.

### PocketID

```nix
oidc = {
  name = "PocketID";
  discoveryUrl = "https://id.example.com/.well-known/openid-configuration";
  clientId = "buildbot";
  clientSecretFile = "/run/secrets/pocketid-secret";
};
```

## Testing with Mock Provider

For local development, you can use `oidc-provider-mock`:

```bash
# Create a client secret file
echo "abc" > /tmp/client_secret

# Start the mock OIDC provider
nix run nixpkgs#pipx -- run oidc-provider-mock \
  --user-claims '{"sub": "alice", "email": "alice@example.com", "name": "Alice", "preferred_username": "alice123"}'
```

Then configure buildbot-nix to use it (in the JSON config file):

```json
"oidc": {
  "name": "Mock",
  "discovery_url": "http://localhost:9400/.well-known/openid-configuration",
  "client_id": "123",
  "client_secret_file": "/tmp/client_secret",
  "scope": ["openid", "email", "profile"],
  "mapping": {
    "email": "email",
    "username": "preferred_username",
    "full_name": "name",
    "groups": null
  }
}
```

## User Identification

Users are identified by their `preferred_username` claim. This means:

- The `admins` list entries must be provider-qualified:
  `oidc:<issuer-host>:<username>` where `<issuer-host>` is the issuer URL
  without the `https://` prefix and `<username>` is the value of the username
  claim
- You can customize which claim is used via `mapping.username`

## Private Repositories

OIDC users have no forge token, so they only see public repositories by default.
`privateRepoViewers` grants visibility (read-only; build control stays with
`admins`):

```nix
services.buildbot-nix.privateRepoViewers = {
  # Repository keys: "forge:owner/repo", "forge:owner/*" or "*";
  # the most specific key wins.
  "*" = [
    # Any authenticated user of this provider.
    "oidc:auth.example.com:*"
  ];
  "gitlab:acme/secret" = [
    # Exact identity or OIDC groups claim.
    "oidc:auth.example.com:alice"
    "oidc:auth.example.com:group:auditors"
  ];
};
```

Group rules need the groups claim in the session: add the `groups` scope and set
`mapping.groups = "groups"`.

The owner segment of a repository key is any namespace: a user, an organization,
or a nested GitLab group (`"gitlab:org/subgroup/*"`).

Members of a GitHub/Gitea organization do not need viewer rules for their own
organization's repositories: they log in with a forge token, and everything that
token can access is visible to them. Viewer rules add visibility on top, for
logins without forge access (OIDC) or for repositories outside a user's own
forge permissions. The OIDC equivalent of an organization is a group rule
(`"oidc:<issuer>:group:<name>"`).

Two caveats around group rules:

- Group membership is captured in the session at login, so revoking a group in
  the identity provider takes effect on the next login or session expiry, not
  immediately.
- Personal API tokens carry an identity but no groups: exact and `provider:*`
  rules apply to API requests, `group:` rules only to browser sessions.

## Groups Support

To sync groups from your OIDC provider:

1. Add the groups scope: `scope = [ "openid" "email" "profile" "groups" ];`
2. Set the groups claim mapping: `mapping.groups = "groups";`

The claim name varies by provider - check your provider's documentation.
