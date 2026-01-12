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

          # Configure outputs directory for testing
          outputsPath = "/var/lib/buildbot-outputs";

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
            nix
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
            # Dynamically get the system
            system=$(nix config show system)
            cat > flake.nix << EOF
            {
              outputs = { self }: {
                checks.$system = {
                  test = derivation {
                    name = "test";
                    system = "$system";
                    builder = "/bin/sh";
                    args = [ "-c" "echo 'Hello from test' > \$out" ];
                  };

                  failing-test = derivation {
                    name = "failing-test";
                    system = "$system";
                    builder = "/bin/sh";
                    args = [ "-c" "echo 'This test will fail' && exit 1" ];
                  };

                  skippable-test = derivation {
                    name = "skippable-test";
                    system = "$system";
                    builder = "/bin/sh";
                    args = [ "-c" "echo 'This test will be skipped' > \$out" ];
                  };
                };

                herculesCI = { ... }: {
                  onPush.default.outputs.effects = {
                    test-effect = {
                      type = "derivation";
                      name = "test-effect";
                      system = "$system";
                      builder = "/bin/sh";
                      args = [ "-c" "echo 'Effect executed successfully!' > \$out" ];
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

            # Pre-build the skippable test to create a cached build
            cd /tmp/test-flake
            nix build .#checks.$system.skippable-test
          '';
        };
      };
  };

  testScript = ''
    import json
    from typing import Any

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

    with subtest("Poller triggers builds and verifies results"):
        # Wait for builds to complete
        def check_builds_complete(_ignore: Any) -> bool:
            builds_json = buildbot.succeed("curl --fail http://localhost:8010/api/v2/builds")
            builds = json.loads(builds_json)

            if len(builds["builds"]) == 0:
                return False

            # Check if all builds are complete
            incomplete_count = sum(1 for build in builds["builds"] if build["results"] is None)

            # Wait for builds to complete (we expect at least 3: test, failing-test, effect)
            # Note: skippable-test won't create a build since it's cached
            if incomplete_count > 0 or len(builds["builds"]) < 3:
                return False

            # Collect build results
            build_results = {}
            for build in builds["builds"]:
                build_id = build["buildid"]
                props_json = buildbot.succeed(f"curl --fail http://localhost:8010/api/v2/builds/{build_id}/properties")
                props_data = json.loads(props_json)

                # Properties are returned as a list containing a single dict
                properties = props_data.get("properties", [{}])[0] if props_data.get("properties") else {}

                # Get the attr or virtual_builder_name to identify the check
                identifier = None
                if "attr" in properties:
                    # Extract the check name from attr like "x86_64-linux.failing-test"
                    attr_value = properties["attr"][0] if isinstance(properties["attr"], list) else properties["attr"]
                    identifier = attr_value.split(".")[-1] if "." in attr_value else attr_value
                elif "virtual_builder_name" in properties:
                    # Extract from virtual_builder_name like "git+ssh:test-flake#checks.x86_64-linux.failing-test"
                    vbn = properties["virtual_builder_name"][0] if isinstance(properties["virtual_builder_name"], list) else properties["virtual_builder_name"]
                    identifier = vbn.split(".")[-1] if "." in vbn else vbn

                if identifier:
                    build_results[identifier] = build["results"]

            # Verify we have the expected results:
            # 1. "test" should succeed (result == 0)
            # 2. "failing-test" should fail (result == 2)
            # 3. "skippable-test" should not be in results (it was cached)

            expected_success = ["test"]
            expected_failure = ["failing-test"]

            for test_name in expected_success:
                assert test_name in build_results, f"Expected successful test '{test_name}' not found in builds"
                assert build_results[test_name] == 0, f"Test '{test_name}' should succeed but got result {build_results[test_name]}"

            for test_name in expected_failure:
                assert test_name in build_results, f"Expected failing test '{test_name}' not found in builds"
                assert build_results[test_name] == 2, f"Test '{test_name}' should fail but got result {build_results[test_name]}"

            # Verify skippable-test is NOT in the results (because it was cached)
            assert "skippable-test" not in build_results, "Test 'skippable-test' should have been skipped but was built"

            return True

        retry(check_builds_complete, timeout_seconds=180)

    with subtest("Verify output paths are written for skipped builds"):
        # Get the system architecture
        system = buildbot.succeed("nix config show system").strip()
        
        # Get the skippable-test output path from nix
        skippable_out_path = buildbot.succeed(f"""
            nix eval --raw /tmp/test-flake#checks.{system}.skippable-test.outPath
        """).strip()
        
        print(f"Expected output path for skippable-test: {skippable_out_path}")
        
        # Check if the output path was written for the skipped build
        # The path should be: /var/lib/buildbot-outputs/<owner>/<repo>/<branch>/<attr>
        # For pull-based projects, owner is "unknown" and repo is the project name
        output_file = f"/var/lib/buildbot-outputs/unknown/test-flake/master/{system}.skippable-test"
        
        # Verify the output file exists and contains the correct path
        buildbot.wait_until_succeeds(f"test -f {output_file}", timeout=30)
        written_path = buildbot.succeed(f"cat {output_file}").strip()
        
        assert written_path == skippable_out_path, f"Output path mismatch: expected {skippable_out_path}, got {written_path}"
  '';
}
