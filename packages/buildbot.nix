{ buildbot, fetchpatch }:
buildbot.overrideAttrs (o: {
  patches = o.patches ++ [
    (fetchpatch {
      name = "add-Build.skipBuildIf.patch";
      url = "https://github.com/buildbot/buildbot/commit/f08eeef96e15c686a4f6ad52368ad08246314751.patch";
      hash = "sha256-ACPYXMbjIfw02gsKwmDKIIZkGSxxLWCaW7ceEcgbtIU=";
    })
    (fetchpatch {
      name = "fix-deprecation-warnings.patch";
      url = "https://github.com/buildbot/buildbot/commit/a894300c5085be925f5021bae2058492625a786b.patch";
      hash = "sha256-agxubz/5PSw4WL3/d63GVnTKy67mLmL9pbVeImvbrYA=";
    })
  ];
})
