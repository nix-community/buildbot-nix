(import ./lib.nix) {
  name = "scheduled-effects";
  nodes = {
    buildbot =
      { self, pkgs, ... }:
      let
        # Test flake with scheduled effects example
        testFlakeSrc = ./test-flake;

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

        services.openssh.enable = true;
        services.openssh.hostKeys = [
          {
            path = "/run/ssh_host_ed25519_key";
            type = "ed25519";
          }
        ];
        users.users.root.openssh.authorizedKeys.keys = [ testPublicKey ];

        programs.ssh.knownHosts.localhost = {
          publicKey = testPublicKey;
        };

        systemd.tmpfiles.rules = [
          "C /run/ssh_host_ed25519_key 0600 root root - ${testPrivateKey}"
        ];

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
            pollInterval = 10;
            repositories = {
              test-flake = {
                url = "ssh://root@localhost/srv/repos/test-flake.git";
                defaultBranch = "master";
                sshPrivateKeyFile = testPrivateKey;
              };
            };
          };
        };

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
          (pkgs.python3.pkgs.callPackage ../packages/buildbot-effects.nix { })
        ];

        networking.firewall.enable = false;

        systemd.services.setup-git-repo = {
          description = "Setup git test repository with scheduled effects";
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

            # Create working directory from test flake source
            rm -rf /tmp/test-flake
            cp -r ${testFlakeSrc} /tmp/test-flake
            chmod -R u+w /tmp/test-flake
            cd /tmp/test-flake
            git init
            git config user.name 'Test User'
            git config user.email 'test@example.com'

            git add flake.nix flake.lock
            git commit -m "Initial commit with scheduled effects"

            # Push to bare repository
            git remote add origin /srv/repos/test-flake.git
            git push -u origin master
          '';
        };
      };
  };

  testScript = ''
    import json

    buildbot.wait_for_unit("sshd.service")
    buildbot.wait_for_unit("setup-git-repo.service")
    buildbot.wait_for_unit("multi-user.target")

    with subtest("Master and worker services start"):
        buildbot.wait_for_unit("buildbot-master.service")
        buildbot.wait_for_unit("buildbot-worker.service")
        buildbot.wait_for_open_port(8010)
        buildbot.wait_until_succeeds("curl --fail --head http://localhost:8010", timeout=60)

    with subtest("Project is registered"):
        projects_json = buildbot.wait_until_succeeds("curl http://localhost:8010/api/v2/projects", timeout=60)
        projects = json.loads(projects_json)
        project_names = [p["name"] for p in projects["projects"]]
        assert "test-flake" in project_names, f"Expected project 'test-flake' in {project_names}"

    with subtest("CLI list-schedules works"):
        # Test the buildbot-effects list-schedules command directly on the repo
        result = buildbot.succeed("""
            cd /tmp/test-flake
            buildbot-effects list-schedules
        """)
        schedules = json.loads(result)

        flake_update = schedules["flake-update"]
        assert flake_update["when"]["hour"] == 4, f"Expected hour=4 for flake-update, got {flake_update['when']}"
        assert flake_update["when"]["dayOfWeek"] == ["Mon"], f"Expected dayOfWeek=['Mon'] for flake-update, got {flake_update['when']}"
        assert "update" in flake_update["effects"], f"Expected 'update' effect in flake-update: {flake_update}"

    with subtest("Push a new commit to trigger build"):
        # GitPoller only detects new commits. We need to push a new commit after buildbot has started.
        buildbot.succeed("""
            cd /tmp/test-flake
            echo "# trigger rebuild" >> flake.nix
            git add flake.nix
            git commit -m "Trigger build"
            git push origin master
        """)

    with subtest("Wait for nix-eval build to complete"):
        def check_eval_build_complete(_ignore):
            builds_json = buildbot.succeed("curl --fail http://localhost:8010/api/v2/builds")
            builds = json.loads(builds_json)
            for build in builds["builds"]:
                if build.get("results") is not None:
                    return True
            return False

        retry(check_eval_build_complete, timeout_seconds=180)

    with subtest("Schedule cache is created"):
        cache_file = "/var/lib/buildbot/scheduled-effects-cache/test-flake-schedules.json"
        buildbot.wait_until_succeeds(f"test -f {cache_file}", timeout=120)

        cache_content = buildbot.succeed(f"cat {cache_file}")
        cached_schedules = json.loads(cache_content)

        assert "flake-update" in cached_schedules, f"Expected 'flake-update' in cache: {cached_schedules}"

    with subtest("Nightly schedulers are created"):
        # The reconfig happens asynchronously after the cache is written (5 second delay)
        # Wait for the Nightly schedulers to appear
        def check_nightly_schedulers_exist(_ignore):
            schedulers_json = buildbot.succeed("curl --fail http://localhost:8010/api/v2/schedulers")
            schedulers = json.loads(schedulers_json)
            scheduler_names = [s["name"] for s in schedulers["schedulers"]]
            return "test-flake-scheduled-flake-update-update" in scheduler_names

        retry(check_nightly_schedulers_exist, timeout_seconds=60)

        schedulers_json = buildbot.succeed("curl --fail http://localhost:8010/api/v2/schedulers")
        schedulers = json.loads(schedulers_json)
        scheduler_names = [s["name"] for s in schedulers["schedulers"]]

        expected_schedulers = [
            "test-flake-scheduled-flake-update-update",
        ]

        for expected in expected_schedulers:
            assert expected in scheduler_names, f"Expected scheduler '{expected}' not found in {scheduler_names}"

    with subtest("Scheduled effect builder exists"):
        builders_json = buildbot.succeed("curl --fail http://localhost:8010/api/v2/builders")
        builders = json.loads(builders_json)
        builder_names = [b["name"] for b in builders["builders"]]

        assert "test-flake/run-scheduled-effect" in builder_names, \
            f"Expected 'test-flake/run-scheduled-effect' builder not found in {builder_names}"
  '';
}
