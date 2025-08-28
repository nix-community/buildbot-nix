(import ./lib.nix) {
  name = "buildbot-nix-poller";
  nodes = {
    buildbot =
      { self, pkgs, ... }:
      let
        # Pre-generated SSH key pair for testing
        testPrivateKey = pkgs.writeText "ssh-key" ''
          -----BEGIN OPENSSH PRIVATE KEY-----
          b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
          QyNTUxOQAAACBG+sEWLfMtuYxA4kvzcEgx8GkX6r7zt+hLnsiedIyX1wAAAJhFK1T9RStU
          /QAAAAtzc2gtZWQyNTUxOQAAACBG+sEWLfMtuYxA4kvzcEgx8GkX6r7zt+hLnsiedIyX1w
          AAAED1I5G8QWiUPUYhutClVIyCYqRZ3MYUj90NtABLcaSPZkb6wRYt8y25jEDiS/NwSDHw
          aRfqvvO36EueyJ50jJfXAAAADnRlc3RAbG9jYWxob3N0AQIDBAUGBw==
          -----END OPENSSH PRIVATE KEY-----
        '';

        testPublicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEb6wRYt8y25jEDiS/NwSDHwaRfqvvO36EueyJ50jJfX test@localhost";
      in
      {
        imports = [
          self.nixosModules.buildbot-master
          self.nixosModules.buildbot-worker
        ];

        # SSH server for git repository
        services.openssh.enable = true;
        services.openssh.hostKeys = [
          {
            path = "/run/ssh_host_ed25519_key";
            type = "ed25519";
          }
        ];
        users.users.root.openssh.authorizedKeys.keys = [ testPublicKey ];

        # Configure known hosts for the SSH client (using the same key as host key)
        programs.ssh.knownHosts.localhost = {
          publicKey = testPublicKey;
        };

        # Copy the SSH key to use as host key with proper permissions
        systemd.tmpfiles.rules = [
          "C /run/ssh_host_ed25519_key 0600 root root - ${testPrivateKey}"
        ];

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

          # Use pull-based configuration with poller
          pullBased = {
            pollInterval = 10; # Poll every 5 seconds for testing
            repositories = {
              test-flake = {
                url = "ssh://root@localhost/srv/repos/test-flake.git";
                defaultBranch = "master";
                sshPrivateKeyFile = testPrivateKey;
              };
            };
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
        ];

        # disable firewall
        networking.firewall.enable = false;

        # Setup git repository service
        systemd.services.setup-git-repo = {
          description = "Setup git test repository";
          wantedBy = [ "multi-user.target" ];
          after = [ "sshd.service" ];

          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            User = "root";
            Environment = "HOME=/root";
          };

          path = with pkgs; [
            git
            coreutils
          ];

          script = ''
            # Create bare repository
            rm -rf /srv/repos/test-flake.git
            mkdir -p /srv/repos
            git init --bare /srv/repos/test-flake.git

            # Create temporary working directory
            rm -rf /tmp/test-flake
            mkdir -p /tmp/test-flake
            cd /tmp/test-flake
            git init
            git config user.name 'Test User'
            git config user.email 'test@example.com'

            # Create a minimal flake with checks and effects that doesn't require network
            cat > flake.nix << 'EOF'
            {
              outputs = { self }: {
                checks.${pkgs.stdenv.hostPlatform.system}.test = derivation {
                  name = "test";
                  system = "${pkgs.stdenv.hostPlatform.system}";
                  builder = "/bin/sh";
                  args = [ "-c" "echo 'Hello from test' > $out" ];
                };

                herculesCI = { ... }: {
                  onPush.default.outputs.effects = {
                    test-effect = {
                      type = "derivation";
                      name = "test-effect";
                      system = "${pkgs.stdenv.hostPlatform.system}";
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

            # Push to bare repository
            git remote add origin /srv/repos/test-flake.git
            git push -u origin master
          '';
        };
      };
  };

  testScript = ''
    import json
    from typing import Any
    from pprint import pprint

    buildbot.wait_for_unit("sshd.service")
    buildbot.wait_for_unit("setup-git-repo.service")
    buildbot.wait_for_unit("multi-user.target")

    with subtest("Master and worker services start"):
        buildbot.wait_for_unit("buildbot-master.service")
        buildbot.wait_for_unit("buildbot-worker.service")
        buildbot.wait_for_open_port(8010)
        buildbot.wait_until_succeeds("curl --fail --head http://localhost:8010", timeout=60)

    # Wait for project to be registered and verify it
    projects_json = buildbot.wait_until_succeeds("curl http://localhost:8010/api/v2/projects", timeout=60)
    projects = json.loads(projects_json)
    project_names = [p["name"] for p in projects["projects"]]
    assert "test-flake" in project_names, f"Expected project 'test-flake' in {project_names}"

    # Wait for the first poll to complete
    with subtest("Wait for poller to initialize"):
        buildbot.wait_until_succeeds(
            'journalctl -u buildbot-master.service | grep "gitpoller: processing changes from"',
            timeout=120
        )
        print("Poller has started processing")

    # Push a new commit to trigger the poller
    with subtest("Push new commit to trigger poller"):
        buildbot.succeed("""
            cd /tmp/test-flake
            echo '# Updated at test time' >> flake.nix
            git add flake.nix
            git commit -m 'Test commit to trigger poller'
            git push origin master
        """)
        print("Pushed new commit to repository")

    with subtest("Poller triggers build automatically"):
        # Wait for build to be triggered and complete successfully
        def check_build_success(_ignore: Any) -> bool:
            builds_json = buildbot.succeed("curl --fail http://localhost:8010/api/v2/builds")
            builds = json.loads(builds_json)
            pprint(builds)
            
            if len(builds["builds"]) == 0:
                print("No builds found - poller hasn't triggered yet")
                return False
            
            build = builds["builds"][0]
            if build["results"] is None:
                print(f"Build still running (state: {build.get('state_string', 'unknown')})")
                return False
            
            result = build["results"]
            print(f"Build completed with result: {result}")
            assert result == 0, f"Build failed with result: {result}"
            return True

        retry(check_build_success, timeout=180)
  '';
}
