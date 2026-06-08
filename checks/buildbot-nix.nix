let
  # Both nodes build the same minimal flake with one check.
  setupTestFlake = ''
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
  '';
in
(import ./lib.nix) {
  name = "buildbot-nix";
  nodes = {
    # GitHub mode against a fake GitHub API: discovery, webhook, eval,
    # build, and commit-status assertions all run against local fakes.
    github =
      { self, pkgs, ... }:
      let
        fakeGithubPort = 8970;
        fakeGithub = pkgs.writers.writePython3Bin "fake-github" { } ''
          import json
          import re
          from http.server import BaseHTTPRequestHandler, HTTPServer

          STATUS_LOG = "/var/lib/fake-github/statuses.jsonl"

          REPO = {
              "id": 1,
              "name": "test-flake",
              "owner": {"login": "acme"},
              "default_branch": "master",
              "clone_url": "file:///var/lib/test-repo",
              "private": False,
              "topics": ["build-with-buildbot"],
          }


          class Handler(BaseHTTPRequestHandler):
              def _json(self, payload, code=200):
                  body = json.dumps(payload).encode()
                  self.send_response(code)
                  self.send_header("Content-Type", "application/json")
                  self.send_header("Content-Length", str(len(body)))
                  self.end_headers()
                  self.wfile.write(body)

              def do_GET(self):
                  path = self.path.split("?")[0]
                  if path == "/app/installations":
                      self._json([{"id": 1}])
                  elif path == "/installation/repositories":
                      self._json({"repositories": [REPO]})
                  elif path == "/repos/acme/test-flake/pulls":
                      # Reconciliation polls open PRs; none exist here.
                      self._json([])
                  else:
                      self._json({"message": "not found"}, 404)

              def do_POST(self):
                  length = int(self.headers.get("Content-Length") or 0)
                  body = self.rfile.read(length)
                  path = self.path.split("?")[0]
                  if re.fullmatch(r"/app/installations/1/access_tokens", path):
                      self._json({"token": "fake-token"}, 201)
                  elif re.fullmatch(r"/repos/acme/test-flake/statuses/[0-9a-f]+", path):
                      entry = json.loads(body)
                      entry["sha"] = path.rsplit("/", 1)[1]
                      with open(STATUS_LOG, "a") as f:
                          f.write(json.dumps(entry) + "\n")
                      self._json({}, 201)
                  else:
                      self._json({"message": "not found"}, 404)

              def log_message(self, fmt, *args):
                  pass


          HTTPServer(("127.0.0.1", ${toString fakeGithubPort}), Handler).serve_forever()
        '';
      in
      {
        imports = [ self.nixosModules.buildbot-nix ];

        services.buildbot-nix = {
          enable = true;
          domain = "localhost";
          nginx.enable = false;
          github = {
            enable = true;
            appId = 123;
            apiUrl = "http://127.0.0.1:${toString fakeGithubPort}";
            appSecretKeyFile = "/var/lib/secrets/github-app-key.pem";
            webhookSecretFile = pkgs.writeText "webhook-secret" "test-webhook-secret";
          };
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

        systemd.services.fake-github = {
          wantedBy = [ "multi-user.target" ];
          before = [ "buildbot-nix.service" ];
          requiredBy = [ "buildbot-nix.service" ];
          serviceConfig = {
            ExecStart = "${fakeGithub}/bin/fake-github";
            StateDirectory = "fake-github";
          };
        };

        systemd.services.setup-test-repo = {
          wantedBy = [ "multi-user.target" ];
          before = [ "buildbot-nix.service" ];
          requiredBy = [ "buildbot-nix.service" ];
          path = [
            pkgs.git
            pkgs.openssl
          ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
          };
          script = ''
            mkdir -p /var/lib/secrets
            openssl genrsa -out /var/lib/secrets/github-app-key.pem 2048
            chmod 644 /var/lib/secrets/github-app-key.pem

            rm -rf /var/lib/test-repo /tmp/test-flake
            ${setupTestFlake}
            git clone --bare /tmp/test-flake /var/lib/test-repo
            chmod -R a+rX /var/lib/test-repo
          '';
        };
      };

    # Gitea mode against a real Gitea: discovery registers the webhook,
    # a push delivers it, and buildbot-nix posts commit statuses back.
    gitea =
      { self, pkgs, ... }:
      {
        imports = [ self.nixosModules.buildbot-nix ];

        services.gitea = {
          enable = true;
          settings = {
            server = {
              HTTP_PORT = 3742;
              ROOT_URL = "http://localhost:3742/";
              DOMAIN = "localhost";
            };
            security.INSTALL_LOCK = true;
            webhook.ALLOWED_HOST_LIST = "localhost";
          };
          database.type = "sqlite3";
        };

        services.buildbot-nix = {
          enable = true;
          domain = "localhost";
          gitea = {
            enable = true;
            instanceUrl = "http://localhost:3742";
            tokenFile = "/var/lib/secrets/gitea-token";
            topic = "buildbot-nix";
          };
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

        networking.firewall.enable = false;

        systemd.services.setup-gitea = {
          wantedBy = [ "multi-user.target" ];
          after = [ "gitea.service" ];
          requires = [ "gitea.service" ];
          before = [ "buildbot-nix.service" ];
          requiredBy = [ "buildbot-nix.service" ];
          path = [
            pkgs.gitea
            pkgs.git
            pkgs.curl
            pkgs.jq
            pkgs.coreutils
            pkgs.util-linux
            pkgs.bash
          ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            Environment = "HOME=/root";
          };
          script = ''
            set -x
            timeout 60 bash -c 'until curl -fs http://localhost:3742/api/v1/version; do sleep 1; done'

            cd /var/lib/gitea
            runuser -u gitea -- \
              env GITEA_WORK_DIR=/var/lib/gitea \
              GITEA_CUSTOM=/var/lib/gitea/custom \
              gitea admin user create \
              --config /var/lib/gitea/custom/conf/app.ini \
              --username gitea-admin \
              --password testpassword123 \
              --email admin@example.com \
              --admin \
              --must-change-password=false

            TOKEN=$(curl -fs -X POST \
              -H "Content-Type: application/json" \
              -d '{"name":"buildbot-nix-token","scopes":["write:repository","write:user","write:organization"]}' \
              -u "gitea-admin:testpassword123" \
              http://localhost:3742/api/v1/users/gitea-admin/tokens | jq -r .sha1)

            mkdir -p /var/lib/secrets
            echo "$TOKEN" > /var/lib/secrets/gitea-token
            chmod 644 /var/lib/secrets/gitea-token
            cp /var/lib/secrets/gitea-token /tmp/gitea-token

            curl -fs -X POST \
              -H "Authorization: token $TOKEN" \
              -H "Content-Type: application/json" \
              -d '{"name":"test-flake","private":false,"default_branch":"master"}' \
              http://localhost:3742/api/v1/user/repos

            curl -fs -X PUT \
              -H "Authorization: token $TOKEN" \
              -H "Content-Type: application/json" \
              -d '{"topics":["buildbot-nix"]}' \
              http://localhost:3742/api/v1/repos/gitea-admin/test-flake/topics

            rm -rf /tmp/test-flake
            ${setupTestFlake}
            git remote add origin http://gitea-admin:testpassword123@localhost:3742/gitea-admin/test-flake.git
            git push -u origin master
          '';
        };
      };
  };

  testScript = ''
    import hashlib
    import hmac
    import json
    import shlex

    start_all()

    with subtest("github: buildbot-nix becomes healthy"):
        github.wait_for_unit("buildbot-nix.service")
        github.wait_until_succeeds(
            "curl --fail -s http://127.0.0.1:8010/health", timeout=120
        )

    with subtest("github: project discovered from fake forge"):
        def github_project_discovered(_ignore):
            out = github.succeed("curl --fail -s http://127.0.0.1:8010/api/repos")
            projects = json.loads(out)
            print(projects)
            return any(
                p["owner"] == "acme" and p["name"] == "test-flake"
                for p in projects
            )

        retry(github_project_discovered, timeout_seconds=120)

    with subtest("github: webhook push triggers eval, build, and statuses"):
        sha = github.succeed("git -C /var/lib/test-repo rev-parse master").strip()
        body = json.dumps({
            "ref": "refs/heads/master",
            "after": sha,
            "repository": {"id": 1, "default_branch": "master"},
            "head_commit": {"message": "initial commit"},
        }).encode()
        sig = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
        github.succeed(
            "curl --fail -s -X POST http://127.0.0.1:8010/webhooks/github "
            "-H 'Content-Type: application/json' "
            "-H 'X-GitHub-Event: push' "
            "-H 'X-GitHub-Delivery: test-delivery-1' "
            f"-H 'X-Hub-Signature-256: sha256={sig}' "
            f"-d {shlex.quote(body.decode())}"
        )

        def github_statuses_posted(_ignore):
            out = github.execute("cat /var/lib/fake-github/statuses.jsonl")[1]
            statuses = [json.loads(line) for line in out.splitlines() if line]
            print(statuses)
            done = {
                s["context"]: s["state"]
                for s in statuses
                if s["state"] in ("success", "failure", "error")
            }
            return (
                done.get("buildbot/nix-eval") == "success"
                and done.get("buildbot/nix-build") == "success"
            )

        retry(github_statuses_posted, timeout_seconds=300)

    with subtest("gitea: buildbot-nix becomes healthy"):
        gitea.wait_for_unit("buildbot-nix.service")
        # The gitea node keeps the default managed nginx vhost; the
        # engine only listens on the unix socket there, so probe
        # through nginx (the github node covers the plain TCP mode).
        gitea.wait_for_unit("nginx.service")
        gitea.wait_until_succeeds(
            "curl --fail -s http://localhost/health", timeout=120
        )

    with subtest("gitea: project discovered and webhook registered"):
        def gitea_hook_registered(_ignore):
            out = gitea.succeed(
                "TOKEN=$(cat /tmp/gitea-token); "
                "curl -fs -H \"Authorization: token $TOKEN\" "
                "http://localhost:3742/api/v1/repos/gitea-admin/test-flake/hooks"
            )
            hooks = json.loads(out)
            print(hooks)
            return any(h.get("active") for h in hooks)

        retry(gitea_hook_registered, timeout_seconds=180)

    with subtest("gitea: push triggers eval, build, and commit statuses"):
        gitea.succeed(
            "cd /tmp/test-flake && "
            "echo '# trigger' >> flake.nix && "
            "git add flake.nix && "
            "git commit -m 'trigger build' && "
            "git push origin master"
        )
        sha = gitea.succeed("git -C /tmp/test-flake rev-parse master").strip()

        def gitea_statuses_posted(_ignore):
            out = gitea.succeed(
                "TOKEN=$(cat /tmp/gitea-token); "
                "curl -fs -H \"Authorization: token $TOKEN\" "
                f"http://localhost:3742/api/v1/repos/gitea-admin/test-flake/statuses/{sha}"
            )
            statuses = json.loads(out)
            print(statuses)
            done = {
                s["context"]: s["status"]
                for s in statuses
                if s["status"] in ("success", "failure", "error")
            }
            return (
                done.get("buildbot/nix-eval") == "success"
                and done.get("buildbot/nix-build") == "success"
            )

        retry(gitea_statuses_posted, timeout_seconds=300)
  '';
}
