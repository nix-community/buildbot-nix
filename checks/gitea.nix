(import ./lib.nix) {
  name = "buildbot-nix-gitea";
  nodes = {
    buildbot =
      { self, pkgs, ... }:
      {
        imports = [
          self.nixosModules.buildbot-master
          self.nixosModules.buildbot-worker
        ];

        # Gitea configuration
        services.gitea = {
          enable = true;
          settings = {
            server = {
              HTTP_PORT = 3742;
              ROOT_URL = "http://localhost:3742/";
              DOMAIN = "localhost";
            };
            service = {
              DISABLE_REGISTRATION = false;
              REQUIRE_SIGNIN_VIEW = false;
            };
            security.INSTALL_LOCK = true;
            webhook.ALLOWED_HOST_LIST = "localhost";
          };
          database.type = "sqlite3";
        };

        # Buildbot master configuration
        services.buildbot-nix.master = {
          enable = true;
          domain = "localhost";
          authBackend = "none";
          workersFile = pkgs.writeText "workers.json" ''
            [
              { "name": "local-worker", "pass": "test-password", "cores": 4 }
            ]
          '';
          admins = [ "admin" ];

          # Gitea integration
          gitea = {
            enable = true;
            webhookSecretFile = pkgs.writeText "webhook-secret" "test-webhook-secret";
            # Token file will be created/updated at runtime
            tokenFile = "/var/lib/buildbot/gitea-token";
            instanceUrl = "http://localhost:3742";

            # Projects configuration
            topic = "buildbot-nix";
          };
        };

        # Buildbot worker configuration
        services.buildbot-nix.worker = {
          enable = true;
          name = "local-worker";
          workerPasswordFile = pkgs.writeText "worker-password" "test-password";
        };

        nix.settings.experimental-features = [
          "nix-command"
          "flakes"
        ];

        environment.systemPackages = with pkgs; [
          buildbot
          curl
          jq
          git
          tea # Gitea CLI client
        ];

        # disable firewall
        networking.firewall.enable = false;

        # Setup Gitea repository and user
        systemd.services.setup-gitea = {
          description = "Setup Gitea test environment";
          wantedBy = [ "multi-user.target" ];
          after = [ "gitea.service" ];
          requires = [ "gitea.service" ];
          before = [ "buildbot-master.service" ];

          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            User = "root";
            Environment = "HOME=/root";
          };

          path = with pkgs; [
            gitea
            git
            curl
            jq
            coreutils
            util-linux # for runuser
          ];

          script = ''
            set -x
            # Wait for Gitea to be available
            timeout 60 ${pkgs.bash}/bin/bash -c 'until curl -s http://localhost:3742/api/v1/version; do sleep 1; done'
            echo "Gitea is ready"

            # Create admin user
            echo "Creating admin user..."
            cd /var/lib/gitea
            runuser -u gitea -- \
              env GITEA_WORK_DIR=/var/lib/gitea \
              GITEA_CUSTOM=/var/lib/gitea/custom \
              ${pkgs.gitea}/bin/gitea admin user create \
              --config /var/lib/gitea/custom/conf/app.ini \
              --username 'gitea-admin' \
              --password 'testpassword123' \
              --email 'admin@example.com' \
              --admin \
              --must-change-password=false

            USER_CREATE_RESULT=$?
            if [ $USER_CREATE_RESULT -ne 0 ]; then
              echo "User creation returned non-zero: $USER_CREATE_RESULT"
              echo "Checking if user already exists..."
            fi

            # Wait for user to be available and check via API
            echo "Verifying user exists via API..."
            for i in {1..10}; do
              USER_CHECK=$(curl -s -u "gitea-admin:testpassword123" http://localhost:3742/api/v1/user 2>&1)
              if echo "$USER_CHECK" | grep -q '"username":"gitea-admin"'; then
                echo "User verified to exist!"
                break
              else
                echo "Attempt $i: User not found yet, waiting..."
                echo "Response: $USER_CHECK"
                sleep 2
              fi
            done

            # Generate access token for API operations
            echo "Creating access token..."
            TOKEN_RESPONSE=$(curl -v -X POST \
              -H "Content-Type: application/json" \
              -d '{"name":"buildbot-token","scopes":["write:repository","write:user","write:admin","write:organization"]}' \
              -u "gitea-admin:testpassword123" \
              http://localhost:3742/api/v1/users/gitea-admin/tokens 2>&1)

            echo "Token response: $TOKEN_RESPONSE"
            TOKEN=$(echo "$TOKEN_RESPONSE" | grep -o '"sha1":"[^"]*' | cut -d'"' -f4)

            if [ -z "$TOKEN" ]; then
              echo "Failed to create access token"
              exit 1
            fi

            echo "Token created successfully: ''${TOKEN:0:10}..."

            # Write token to the file that buildbot will use
            mkdir -p /var/lib/buildbot
            echo "$TOKEN" > /var/lib/buildbot/gitea-token
            # Don't chown yet - buildbot user might not exist
            chmod 644 /var/lib/buildbot/gitea-token

            # Also save for testing
            echo "$TOKEN" > /tmp/gitea-token

            # Create repository via API
            echo "Creating repository test-flake..."
            REPO_RESPONSE=$(curl -v -X POST \
              -H "Authorization: token $TOKEN" \
              -H "Content-Type: application/json" \
              -H "accept: application/json" \
              -d '{"name":"test-flake","private":false}' \
              http://localhost:3742/api/v1/user/repos 2>&1)

            echo "Repository creation response: $REPO_RESPONSE"

            if echo "$REPO_RESPONSE" | grep -q '"id"'; then
              echo "Repository created successfully"
            else
              echo "Failed to create repository - it may already exist or there was an error"
            fi

            # Add buildbot-nix topic to the repository
            echo "Adding buildbot-nix topic to repository..."
            TOPIC_RESPONSE=$(curl -v -X PUT \
              -H "Authorization: token $TOKEN" \
              -H "Content-Type: application/json" \
              -d '{"topics":["buildbot-nix"]}' \
              http://localhost:3742/api/v1/repos/gitea-admin/test-flake/topics 2>&1)

            echo "Topic response: $TOPIC_RESPONSE"

            # Clone and setup test repository
            rm -rf /tmp/test-flake
            mkdir -p /tmp/test-flake
            cd /tmp/test-flake
            git init
            git config user.name 'Test User'
            git config user.email 'test@example.com'

            # Create a minimal flake with checks
            cat > flake.nix << 'EOF'
            {
              outputs = { self }: {
                checks.x86_64-linux.test = derivation {
                  name = "test";
                  system = "x86_64-linux";
                  builder = "/bin/sh";
                  args = [ "-c" "echo 'Hello from test' > $out" ];
                };

                herculesCI = { ... }: {
                  onPush.default.outputs.effects = {
                    test-effect = {
                      type = "derivation";
                      name = "test-effect";
                      system = "x86_64-linux";
                      builder = "/bin/sh";
                      args = [ "-c" "echo 'Effect executed successfully!' > $out" ];
                      outputs = [ "out" ];
                    };
                  };
                };
              };
            }
            EOF

            git add flake.nix
            git commit -m "Initial commit"

            # Verify repository exists before pushing
            echo "Verifying repository exists..."
            REPO_CHECK=$(curl -s -H "Authorization: token $TOKEN" \
              http://localhost:3742/api/v1/repos/gitea-admin/test-flake)

            if echo "$REPO_CHECK" | grep -q '"id"'; then
              echo "Repository confirmed to exist, pushing code..."
              # Push to Gitea repository
              git remote add origin http://gitea-admin:testpassword123@localhost:3742/gitea-admin/test-flake.git
              git push -u origin master
            else
              echo "ERROR: Repository does not exist!"
              echo "Response: $REPO_CHECK"
              exit 1
            fi
          '';
        };

        # Ensure buildbot-master starts after gitea setup completes
        systemd.services.buildbot-master = {
          after = [ "setup-gitea.service" ];
          requires = [ "setup-gitea.service" ];
        };
      };
  };

  testScript = ''
    import json
    from typing import Any
    from pprint import pprint

    buildbot.wait_for_unit("gitea.service")
    buildbot.wait_for_unit("setup-gitea.service")
    buildbot.wait_for_unit("multi-user.target")

    with subtest("Gitea service is accessible"):
        buildbot.wait_for_open_port(3742)
        buildbot.wait_until_succeeds(
            "curl --fail http://localhost:3742/api/v1/version",
            timeout=60
        )

    with subtest("Gitea token was created"):
        buildbot.succeed("test -f /var/lib/buildbot/gitea-token")
        token = buildbot.succeed("cat /tmp/gitea-token").strip()
        print(f"Token created: {token[:10]}...")

    with subtest("Master and worker services start"):
        buildbot.wait_for_unit("buildbot-master.service")
        buildbot.wait_for_unit("buildbot-worker.service")
        buildbot.wait_for_open_port(8010)
        buildbot.wait_until_succeeds("curl --fail -v --head http://localhost:8010", timeout=60)

    with subtest("Project is registered in buildbot"):
        def check_project_registered(_ignore):
            projects_json = buildbot.wait_until_succeeds("curl --fail -v http://localhost:8010/api/v2/projects")
            projects = json.loads(projects_json)
            pprint(projects)
            project_names = [p["name"] for p in projects["projects"]]
            if "gitea-admin/test-flake" not in project_names:
                print(f"Project not yet registered, current projects: {project_names}")
                # Trigger manual discovery if needed
                return False
            return True
        
        retry(check_project_registered, timeout_seconds=120)

    with subtest("Verify webhook was created by buildbot"):
        # Wait for buildbot to create the webhook
        buildbot.wait_until_succeeds("""
            TOKEN=$(cat /tmp/gitea-token)
            curl -s -H "Authorization: token $TOKEN" \
                 http://localhost:3742/api/v1/repos/gitea-admin/test-flake/hooks | \
                 jq '.[0].active' | grep true
        """, timeout=60)

    with subtest("Push new commit to trigger webhook"):
        buildbot.succeed("""
            cd /tmp/test-flake
            echo '# Updated via webhook test' >> flake.nix
            git add flake.nix
            git commit -m 'Test commit to trigger webhook'
            git push origin master
        """)

    with subtest("Webhook triggers build automatically"):
        # Wait for build to be triggered and complete successfully
        def check_build_success(_ignore: Any) -> bool:
            builds_json = buildbot.succeed("curl --fail -v http://localhost:8010/api/v2/builds")
            builds = json.loads(builds_json)
            pprint(builds)
            
            if len(builds["builds"]) == 0:
                print("No builds found - webhook hasn't triggered yet")
                return False
            
            build = builds["builds"][0]
            if build["results"] is None:
                print(f"Build still running (state: {build.get('state_string', 'unknown')})")
                return False
            
            result = build["results"]
            print(f"Build completed with result: {result}")
            assert result == 0, f"Build failed with result: {result}"
            return True

        retry(check_build_success, timeout_seconds=180)
  '';
}
