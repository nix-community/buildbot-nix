# OIDC Authentication

buildbot-nix supports generic OpenID Connect (OIDC) authentication, allowing you to use any OIDC-compliant identity provider (Keycloak, PocketID, Authentik, etc.) for user login.

## NixOS Configuration

```nix
{
  services.buildbot-nix.master = {
    authBackend = "oidc";

    # Users identified by their OIDC `preferred_username` claim
    admins = [ "alice" "bob" ];

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

Set your OIDC provider's callback URL to: `https://buildbot.example.com/auth/login`

## Python Configuration

For non-NixOS setups or local development:

```python
from buildbot_nix.models import (
    AuthBackendConfig,
    OIDCConfig,
    OIDCMappingConfig,
)

buildbot_nix_config = BuildbotNixConfig(
    # ... other config ...
    admins=["alice", "bob"],
    auth_backend=AuthBackendConfig.oidc,
    oidc=OIDCConfig(
        name="My Identity Provider",
        discovery_url="https://keycloak.example.com/realms/myrealm/.well-known/openid-configuration",
        client_id="buildbot",
        client_secret_file="/path/to/client_secret",
        scope=["openid", "email", "profile"],
        mapping=OIDCMappingConfig(
            email="email",
            username="preferred_username",
            full_name="name",
            groups=None,  # Or "groups" for group sync
        ),
    ),
)
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

Then configure buildbot-nix to use it:

```python
oidc=OIDCConfig(
    name="Mock",
    discovery_url="http://localhost:9400/.well-known/openid-configuration",
    client_id="123",
    client_secret_file="/tmp/client_secret",
    scope=["openid", "email", "profile"],
    mapping=OIDCMappingConfig(
        email="email",
        username="preferred_username",
        full_name="name",
        groups=None,
    ),
),
```

## User Identification

Users are identified by their `preferred_username` claim. This means:

- The `admins` list should contain usernames as they appear in the OIDC provider
- You can customize which claim is used via `mapping.username`

## Groups Support

To sync groups from your OIDC provider:

1. Add the groups scope: `scope = [ "openid" "email" "profile" "groups" ];`
2. Set the groups claim mapping: `mapping.groups = "groups";`

The claim name varies by provider - check your provider's documentation.
