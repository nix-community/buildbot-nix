{ buildbot, fetchpatch }:
buildbot.overrideAttrs (o: {
  patches = o.patches ++ [
    (fetchpatch {
      name = "add-Build.skipBuildIf.patch";
      url = "https://github.com/buildbot/buildbot/commit/f08eeef96e15c686a4f6ad52368ad08246314751.patch";
      hash = "sha256-ACPYXMbjIfw02gsKwmDKIIZkGSxxLWCaW7ceEcgbtIU=";
    })
  ];
})
