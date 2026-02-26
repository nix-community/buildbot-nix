# Keycloak OIDC (fullyPrivate mode)

buildbot-nix can put the entire instance behind oauth2-proxy using Keycloak as
the identity provider. This uses the `fullyPrivate` access mode, which means
unauthenticated users cannot see anything at all.

This is separate from the `authBackend = "oidc"` option, which only protects
write actions and still exposes the UI to anonymous readers.

## Step 1: Create a Keycloak client

Follow the steps under "Keycloak new admin console (default as of v19.0.0)" in
the
[oauth2-proxy Keycloak OIDC provider docs](https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/keycloak_oidc/).

Set the redirect URI to `https://buildbot.example.com/oauth2/callback`.

Disable "Full scope allowed" on the client to avoid bloating the token with
every realm role. If the token is too large, oauth2-proxy's response headers
will exceed nginx's default buffer size and you will get a 502 after a
successful Keycloak login on the redirect back to buildbot.

Note the client ID and client secret.

## Step 2: Configure buildbot-nix

```nix
services.buildbot-nix.master = {
  # ...

  httpBasicAuthPasswordFile = pkgs.writeText "http-basic-auth-password" "changeMe";

  accessMode.fullyPrivate = {
    backend = "keycloak-oidc";
    issuerUrl = "https://keycloak.example.com/realms/myrealm";
    clientId = "buildbot";
    clientSecretFile = pkgs.writeText "oauth2-proxy-client-secret" "changeMe";
    cookieSecretFile = pkgs.writeText "oauth2-proxy-cookie-secret" "changeMe";

    # Optional: restrict by role or group
    # allowedRoles = [ "buildbot-access" ];
    # allowedGroups = [ "/engineering" ];

    # Optional: restrict by email domain (default: "*")
    # emailDomain = "example.com";

    # Optional: override the OAuth2 scope (default: "openid email profile")
    # scope = "openid email profile groups";
  };
};
```

## Secrets

Three secret files are required:

- `clientSecretFile` -- the Keycloak client secret from step 1.
- `cookieSecretFile` -- a random string used to encrypt the oauth2-proxy session
  cookie. Generate with `openssl rand -base64 32`.
- `httpBasicAuthPasswordFile` -- a shared secret between oauth2-proxy and
  buildbot. oauth2-proxy forwards the authenticated username to buildbot using
  HTTP basic auth with this password. Generate with `openssl rand -hex 32`.

## How it works

1. nginx proxies all requests to oauth2-proxy.
2. oauth2-proxy redirects unauthenticated users to Keycloak.
3. After login, oauth2-proxy sets an HTTP basic auth header with the
   authenticated username and forwards the request to buildbot.
4. buildbot sees the user as authenticated via `httpbasicauth`.

Webhook endpoints (`/change_hook`) skip authentication so that Gitea/GitHub can
still deliver events.

## Role and group filtering

`allowedRoles` maps to oauth2-proxy's `--allowed-role` flag. Use the role name
directly for realm roles, or `client-id:role-name` for client roles.

`allowedGroups` maps to `--allowed-group`. Groups require a "groups" client
scope with a Group Membership mapper in Keycloak (token claim name: `groups`).

When neither is set, any user who can authenticate against the Keycloak realm is
granted access.
