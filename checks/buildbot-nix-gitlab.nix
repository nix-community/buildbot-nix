# Real GitLab is heavyweight (multi-GB closure, minutes of startup),
# hence a check separate from buildbot-nix.nix. Flow under test:
# PAT-based discovery, webhook auto-registration, push-triggered
# eval/build, commit statuses posted back.
#
# Deliberately a qemu VM, not an nspawn container: buildbot-nix runs nix
# builds, and test containers share the host store read-only without a
# nix database.
(import ./lib.nix) {
  name = "buildbot-nix-gitlab";
  nodes.gitlab =
    {
      self,
      pkgs,
      lib,
      ...
    }:
    {
      imports = [ self.nixosModules.buildbot-nix ];

      # Restart loops only mask startup failures in a test.
      systemd.services.gitlab.serviceConfig.Restart = lib.mkForce "no";
      systemd.services.gitlab-workhorse.serviceConfig.Restart = lib.mkForce "no";
      systemd.services.gitaly.serviceConfig.Restart = lib.mkForce "no";
      systemd.services.gitlab-sidekiq.serviceConfig.Restart = lib.mkForce "no";

      services.gitlab = {
        enable = true;
        host = "gitlab";
        # Advertised in clone URLs (http_url_to_repo); must match the
        # nginx vhost, not the internal puma port (default 8080).
        port = 80;
        initialRootPasswordFile = pkgs.writeText "root-password" "notproduction";
        secrets = {
          secretFile = pkgs.writeText "secret" "Aig5zaic";
          otpFile = pkgs.writeText "otpsecret" "Riew9mue";
          dbFile = pkgs.writeText "dbsecret" "we2quaeZ";
          jwsFile = pkgs.runCommand "oidcKeyBase" { } "${pkgs.openssl}/bin/openssl genrsa 2048 > $out";
          activeRecordPrimaryKeyFile = pkgs.writeText "arprimary" "vsaYPZjmJ3a14gqLUnOQ";
          activeRecordDeterministicKeyFile = pkgs.writeText "ardeterministic" "DOROVzNNM9PXebBROBnL";
          activeRecordSaltFile = pkgs.writeText "arsalt" "QlPCMjGLtRYXf1ssAGav";
        };
        sidekiq.concurrency = 1;
      };

      services.nginx = {
        enable = true;
        recommendedProxySettings = true;
        virtualHosts.gitlab.locations."/".proxyPass = "http://unix:/run/gitlab/gitlab-workhorse.socket";
      };
      # IPv4-only aliases: "localhost" resolves to ::1 first, but the
      # buildbot-nix listens on 0.0.0.0, so GitLab's webhook delivery to
      # localhost would die on connection refused.
      networking.hosts."127.0.0.1" = [
        "gitlab"
        "buildbot"
      ];

      services.buildbot-nix = {
        enable = true;
        domain = "localhost";
        nginx.enable = false;
        # Port 80 is the GitLab vhost; deliver webhooks straight to the
        # service under its IPv4 alias.
        webhookBaseUrl = "http://buildbot:8010";
        gitlab = {
          enable = true;
          instanceUrl = "http://gitlab";
          tokenFile = "/var/lib/secrets/gitlab-token";
        };
      };
      # The token is minted at runtime; don't fail before it exists.
      systemd.services.buildbot-nix = {
        after = [ "setup-gitlab.service" ];
        requires = [ "setup-gitlab.service" ];
      };

      nix.settings.experimental-features = [
        "nix-command"
        "flakes"
      ];

      environment.systemPackages = [
        pkgs.git
        pkgs.curl
        pkgs.jq
      ];

      systemd.services.setup-gitlab = {
        wantedBy = [ "multi-user.target" ];
        after = [ "gitlab.service" ];
        path = [
          pkgs.curl
          pkgs.jq
          pkgs.git
          pkgs.coreutils
          pkgs.bash
          pkgs.util-linux
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          TimeoutStartSec = "20min";
          Environment = "HOME=/root";
        };
        script = ''
          set -x
          timeout 900 bash -c 'until curl -fs http://gitlab/users/sign_in > /dev/null; do sleep 5; done'

          BEARER=$(curl -fs -X POST -H 'Content-Type: application/json' \
            -d '{"grant_type":"password","username":"root","password":"notproduction"}' \
            http://gitlab/oauth/token | jq -r .access_token)
          AUTH="Authorization: Bearer $BEARER"

          # GitLab refuses webhooks to local addresses by default. The
          # setting is Rails-cached for a minute and earlier requests
          # already cached the default, so drop the cache afterwards.
          curl -fs -X PUT -H "$AUTH" \
            "http://gitlab/api/v4/application/settings?allow_local_requests_from_web_hooks_and_services=true" > /dev/null
          runuser -u gitlab -- /run/current-system/sw/bin/gitlab-rails runner 'Rails.cache.clear'

          # buildbot-nix authenticates with a PAT (PRIVATE-TOKEN), not OAuth.
          # expires_at is mandatory-bounded (max 400 days); a week outlives the test.
          TOKEN=$(curl -fs -X POST -H "$AUTH" -H 'Content-Type: application/json' \
            -d "{\"name\":\"buildbot-nix\",\"scopes\":[\"api\"],\"expires_at\":\"$(date -d '+7 days' +%F)\"}" \
            http://gitlab/api/v4/users/1/personal_access_tokens | jq -r .token)
          test -n "$TOKEN" && test "$TOKEN" != null
          mkdir -p /var/lib/secrets
          echo "$TOKEN" > /var/lib/secrets/gitlab-token
          chmod 644 /var/lib/secrets/gitlab-token

          # The legacy-import topic enables the project on first discovery.
          curl -fs -X POST -H "PRIVATE-TOKEN: $TOKEN" -H 'Content-Type: application/json' \
            -d '{"name":"test-flake","visibility":"public","topics":["build-with-buildbot"]}' \
            http://gitlab/api/v4/projects > /dev/null

          mkdir -p /tmp/test-flake
          cd /tmp/test-flake
          git init -b master
          git config user.name test
          git config user.email test@example.com
          cat > flake.nix <<'EOF'
          {
            outputs = { self }: {
              checks.x86_64-linux.test = derivation {
                name = "test";
                system = "x86_64-linux";
                builder = "/bin/sh";
                args = [ "-c" "echo hello > $out" ];
              };
            };
          }
          EOF
          git add flake.nix
          git commit -m "initial commit"
          git remote add origin "http://oauth2:$TOKEN@gitlab/root/test-flake.git"
          git push -u origin master
        '';
      };

      virtualisation.memorySize = 6144;
      virtualisation.cores = 4;
      virtualisation.useNixStoreImage = true;
      virtualisation.writableStore = true; # buildbot-nix builds into the store
    };

  testScript = ''
    import json

    start_all()

    gitlab.wait_for_unit("setup-gitlab.service", timeout=1800)
    gitlab.wait_for_unit("buildbot-nix.service")
    gitlab.wait_until_succeeds(
        "curl --fail -s http://127.0.0.1:8010/health", timeout=120
    )

    with subtest("project discovered and webhook registered"):
        def hook_registered(_ignore):
            out = gitlab.succeed(
                "TOKEN=$(cat /var/lib/secrets/gitlab-token); "
                'curl -fs -H "PRIVATE-TOKEN: $TOKEN" '
                "http://gitlab/api/v4/projects/root%2Ftest-flake/hooks"
            )
            hooks = json.loads(out)
            print(hooks)
            return any(
                h["url"].endswith("/webhooks/gitlab")
                and h["push_events"]
                and h["merge_requests_events"]
                for h in hooks
            )

        retry(hook_registered, timeout_seconds=300)

    with subtest("push delivers the webhook"):
        gitlab.succeed(
            "cd /tmp/test-flake && "
            "echo '# trigger' >> flake.nix && "
            "git add flake.nix && git commit -m 'trigger build' && "
            "git push origin master"
        )
        sha = gitlab.succeed("git -C /tmp/test-flake rev-parse master").strip()

        # Early cutoff: a build for the pushed commit must exist before
        # waiting minutes on statuses, otherwise delivery is broken.
        def build_created(_ignore):
            out = gitlab.succeed(
                "curl --fail -s "
                f"'http://127.0.0.1:8010/api/repos/gitlab/root/test-flake/builds?commit={sha}'"
            )
            builds = json.loads(out)["items"]
            print(builds)
            return bool(builds)

        try:
            retry(build_created, timeout_seconds=120)
        except Exception:
            print(gitlab.execute("journalctl -u buildbot-nix --no-pager | tail -50")[1])
            print(gitlab.execute(
                "runuser -u gitlab -- /run/current-system/sw/bin/gitlab-rails runner "
                "'puts WebHookLog.last(5).map { |l| "
                "[l.url, l.response_status, l.internal_error_message, l.response_body].inspect }'"
            )[1])
            raise

    with subtest("eval and build post commit statuses"):

        def statuses_posted(_ignore):
            out = gitlab.succeed(
                "TOKEN=$(cat /var/lib/secrets/gitlab-token); "
                'curl -fs -H "PRIVATE-TOKEN: $TOKEN" '
                f"http://gitlab/api/v4/projects/root%2Ftest-flake/repository/commits/{sha}/statuses"
            )
            statuses = json.loads(out)
            print(statuses)
            done = {
                s["name"]: s["status"]
                for s in statuses
                if s["status"] in ("success", "failed")
            }
            return (
                done.get("buildbot/nix-eval") == "success"
                and done.get("buildbot/nix-build") == "success"
            )

        retry(statuses_posted, timeout_seconds=600)
  '';
}
