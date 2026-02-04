## services\.buildbot-nix\.packages\.buildbot



The buildbot package to use\.



*Type:*
package



*Default:*

```nix
pkgs.callPackage ../packages/buildbot.nix { }
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/packages\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/packages.nix)



## services\.buildbot-nix\.packages\.buildbot-effects



The buildbot-effects package to use\.



*Type:*
package



*Default:*

```nix
python.pkgs.callPackage ../packages/buildbot-effects.nix { }
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/packages\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/packages.nix)



## services\.buildbot-nix\.packages\.buildbot-gitea



The buildbot-gitea package to use\.



*Type:*
package



*Default:*

```nix
python.pkgs.callPackage ../packages/buildbot-gitea.nix { }
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/packages\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/packages.nix)



## services\.buildbot-nix\.packages\.buildbot-nix



The buildbot-nix package to use\.



*Type:*
package



*Default:*

```nix
python.pkgs.callPackage ../packages/buildbot-nix.nix { }
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/packages\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/packages.nix)



## services\.buildbot-nix\.packages\.buildbot-plugins



Attrset of buildbot plugin packages to use\.



*Type:*
attribute set of package



*Default:*

```nix
pkgs.buildbot-plugins
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/packages\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/packages.nix)



## services\.buildbot-nix\.packages\.buildbot-worker



The buildbot-worker package to use\.



*Type:*
package



*Default:*

```nix
pkgs.buildbot-worker
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/packages\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/packages.nix)



## services\.buildbot-nix\.packages\.python



Python interpreter to use for buildbot-nix\.



*Type:*
package



*Default:*

```nix
config.services.buildbot-nix.packages.buildbot.python
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/packages\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/packages.nix)



## services\.buildbot-nix\.master\.enable



Whether to enable buildbot-master\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.accessMode

Controls the access mode for the Buildbot instance\. Choose between public (default) or fullyPrivate mode\.



*Type:*
attribute-tagged union with choices: fullyPrivate, public



*Default:*

```nix
{
  public = { };
}
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate



Puts the buildbot instance behind \`oauth2-proxy’ which protects the whole instance\. This makes
buildbot-native authentication unnecessary unless one desires a mode where the team that can access
the instance read-only is a superset of the the team that can access it read-write\.



*Type:*
submodule

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate\.backend



Which backend to use for authentication\. User’s will have to authenticate with this backend,
to even gain access to a read-only view of this buildbot\.



*Type:*
one of “gitea”, “github”



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate\.clientId



Client secret used for OAuth2 authentication\.



*Type:*
string



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate\.clientSecretFile



Path to a file containing the client secret\.



*Type:*
absolute path



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate\.cookieSecretFile



Path to a file containing the cookie secret\.



*Type:*
absolute path



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate\.port



Port number at which the \`oauth2-proxy’ will listen on\.



*Type:*
16 bit unsigned integer; between 0 and 65535 (both inclusive)



*Default:*

```nix
8020
```



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate\.teams



A list of teams that should be given access to BuildBot\.



*Type:*
list of string



*Default:*

```nix
[ ]
```



## services\.buildbot-nix\.master\.accessMode\.fullyPrivate\.users



A list of users that should be given access to BuildBot\.



*Type:*
list of string



*Default:*

```nix
[ ]
```



## services\.buildbot-nix\.master\.accessMode\.public



Default public mode, will allow read only access to anonymous users\. Authentication is handled by
one of the \`authBackend’s\. CAUTION this will leak information about private repos, the instance has
access to\. Information includes, but is not limited to, repository URLs, number and name of checks,
and build logs



*Type:*
submodule

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.admins



Users that are allowed to login to buildbot, trigger builds and change settings



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.allowUnauthenticatedControl



Whether to enable allowing unauthenticated users to perform control actions (cancel, restart,
force builds)\. Useful when running buildbot behind a VPN or on a local network
where network-level access implies trust
\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.authBackend



Which OAuth2 backend to use\.



*Type:*
one of “github”, “gitea”, “httpbasicauth”, “oidc”, “none”



*Default:*

```nix
"github"
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.branches



An attrset of branch rules, each rule specifies which branches it should apply to using the
` matchGlob ` option and then the corresponding settings are applied to the matched branches\.
If multiple rules match a given branch, the rules are ` or `-ed together, by ` or `-ing each
individual boolean option of all matching rules\. Take the following as example:

```
   {
     rule1 = {
       matchGlob = "f*";
       registerGCRroots = false;
       updateOutputs = false;
     }
     rule2 = {
       matchGlob = "foo";
       registerGCRroots = true;
       updateOutputs = false;
     }
   }
```

This example will result in ` registerGCRoots ` both being considered ` true `,
but ` updateOutputs ` being ` false ` for the branch ` foo `\.

The default branches of all repos are considered to be matching of a rule setting all the options
to ` true `\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  rule1 = {
    matchGlob = "f*";
    registerGCRroots = false;
    updateOutputs = false;
  }
  rule2 = {
    matchGlob = "foo";
    registerGCRroots = true;
    updateOutputs = false;
  }
}

```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.branches\.\<name>\.matchGlob



A glob specifying which branches to apply this rule to\.



*Type:*
string

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.branches\.\<name>\.registerGCRoots



Whether to register gcroots for branches matching this glob\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.branches\.\<name>\.updateOutputs



Whether to update outputs for branches matching this glob\.



*Type:*
boolean



*Default:*

```nix
false
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.buildSystems



Systems that we will be build



*Type:*
list of string



*Default:*

```nix
"[ pkgs.stdenv.hostPlatform.system ]"
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.cacheFailedBuilds



Whether to enable cache failed builds in local database to avoid retrying them\.
When enabled, failed builds will be remembered and skipped in subsequent evaluations
unless explicitly rebuilt\. When disabled (the default), all builds will be attempted
regardless of previous failures
\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.cachix\.enable



Whether to enable Enable Cachix integration\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.cachix\.auth



Authentication method for Cachix\. Choose either signingKey or authToken\.



*Type:*
attribute-tagged union with choices: authToken, signingKey

*Declared by:*
 - [\<buildbot-nix/nixosModules/cachix\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/cachix.nix)



## services\.buildbot-nix\.master\.cachix\.auth\.authToken



Use an authentication token to authenticate with Cachix\.



*Type:*
submodule

*Declared by:*
 - [\<buildbot-nix/nixosModules/cachix\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/cachix.nix)



## services\.buildbot-nix\.master\.cachix\.auth\.authToken\.file



Path to a file containing the authentication token\.



*Type:*
absolute path



## services\.buildbot-nix\.master\.cachix\.auth\.signingKey



Use a signing key to authenticate with Cachix\.



*Type:*
submodule

*Declared by:*
 - [\<buildbot-nix/nixosModules/cachix\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/cachix.nix)



## services\.buildbot-nix\.master\.cachix\.auth\.signingKey\.file



Path to a file containing the signing key\.



*Type:*
absolute path



## services\.buildbot-nix\.master\.cachix\.name



Cachix name



*Type:*
string

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.dbUrl



Postgresql database url



*Type:*
string



*Default:*

```nix
"postgresql://@/buildbot"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.domain



Buildbot domain



*Type:*
string



*Example:*

```nix
"buildbot.numtide.com"
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.effects\.perRepoSecretFiles



Per-repository or per-organization secrets files for buildbot effects\.
The secrets themselves need to be valid JSON files\.

Keys can be:

 - Org secrets via wildcard: “github:org/*" or "gitea:org/*”
 - Exact repo match: “github:owner/repo” or “gitea:owner/repo”
   (overrides org secrets)



*Type:*
attribute set of absolute path



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  "github:nix-community/*" = config.agenix.secrets.nix-community-effects.path;
  "github:nix-community/buildbot-nix" = config.agenix.secrets.buildbot-nix-effects.path;
}

```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.evalMaxMemorySize



Maximum memory size for nix-eval-jobs (in MiB) per
worker\. After the limit is reached, the worker is
restarted\.



*Type:*
signed integer



*Default:*

```nix
2048
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.evalWorkerCount



Number of nix-eval-jobs worker processes\. If null, the number of cores is used\.
If you experience memory issues (buildbot-workers going out-of-memory), you can reduce this number\.



*Type:*
null or signed integer



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.failedBuildReportLimit



The maximum number of failed builds per ` nix-eval ` that ` buildbot-nix ` will report individually
to backends (GitHub, Gitea, etc\.) before stopping individual notifications\.

Note: 3 notification slots should always be reserved for nix-eval, nix-build, and nix-effects stages\.

Report failed builds individually as long as the number of failed builds is less than or equal
to this limit\. Once exceeded, individual build notifications are suppressed to avoid hitting
GitHub’s status API limits\. Successful builds never generate individual notifications\.



*Type:*
unsigned integer, meaning >=0



*Default:*

```nix
47
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.enable



Whether to enable Enable Gitea integration\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.gitea\.instanceUrl



Gitea instance URL



*Type:*
string

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.gitea\.oauthId



Gitea oauth id\. Used for the login button



*Type:*
null or string



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.oauthSecretFile



Gitea oauth secret file



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.repoAllowlist



If non-null, specifies an explicit set of repositories that are allowed to use buildbot, i\.e\. buildbot-nix will ignore any repositories not in this list\.



*Type:*
null or (list of string)



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.sshKnownHostsFile



If non-null the specified known hosts file will be matched against when connecting to
repositories over SSH\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.sshPrivateKeyFile



If non-null the specified SSH key will be used to fetch all configured repositories\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.tokenFile



Gitea token file



*Type:*
absolute path

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.topic



Projects that have this topic will be built by buildbot\.
If null, all projects that the buildbot Gitea user has access to, are built\.



*Type:*
null or string



*Default:*

```nix
"build-with-buildbot"
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.gitea\.userAllowlist



If non-null, specifies users/organizations that are allowed to use buildbot, i\.e\. buildbot-nix will ignore any repositories not owned by these users/organizations\.



*Type:*
null or (list of string)



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.gitea\.webhookSecretFile



Gitea webhook secret file



*Type:*
absolute path

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.github\.enable



Whether to enable Enable GitHub integration\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.github\.appId



GitHub app ID\.



*Type:*
signed integer

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.github\.appSecretKeyFile



GitHub app secret key file location\.



*Type:*
absolute path

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.github\.oauthId



Github oauth id\. Used for the login button



*Type:*
null or string



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.github\.oauthSecretFile



Github oauth secret file



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.github\.repoAllowlist



If non-null, specifies an explicit set of repositories that are allowed to use buildbot, i\.e\. buildbot-nix will ignore any repositories not in this list\.



*Type:*
null or (list of string)



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.github\.topic



Projects that have this topic will be built by buildbot\.
If null, all projects that the buildbot github user has access to, are built\.



*Type:*
null or string



*Default:*

```nix
"build-with-buildbot"
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.github\.userAllowlist



If non-null, specifies users/organizations that are allowed to use buildbot, i\.e\. buildbot-nix will ignore any repositories not owned by these users/organizations\.



*Type:*
null or (list of string)



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.github\.webhookSecretFile



Github webhook secret file



*Type:*
absolute path

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.httpBasicAuthPasswordFile



Path to file containing the password used in HTTP basic authentication\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.niks3\.enable



Whether to enable Enable niks3 integration\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.niks3\.package



The niks3 package to use\. You must add the niks3 flake input and overlay to make this package available\.



*Type:*
package

*Declared by:*
 - [\<buildbot-nix/nixosModules/niks3\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/niks3.nix)



## services\.buildbot-nix\.master\.niks3\.authTokenFile



Path to a file containing the niks3 API authentication token\.



*Type:*
absolute path

*Declared by:*
 - [\<buildbot-nix/nixosModules/niks3\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/niks3.nix)



## services\.buildbot-nix\.master\.niks3\.serverUrl



niks3 server URL



*Type:*
string



*Example:*

```nix
"https://niks3.yourdomain.com"
```

*Declared by:*
 - [\<buildbot-nix/nix/common-options\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nix/common-options.nix)



## services\.buildbot-nix\.master\.oidc\.clientId



Client ID of the OIDC client Buildbot should use\.



*Type:*
null or string



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.clientSecretFile



Path to file containing the client secret\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.discoveryUrl



URL for the OIDC discovery endpoint\.
For Keycloak, this would be https://keycloak\.my-project\.com/realms/{realm-name}/\.well-known/openid-configuration\.
For PocketID, this would be https://id\.my-project\.com/\.well-known/openid-configuration\.

Set your client’s callback url to https://buildbot\.my-project\.com/auth/login



*Type:*
null or string



*Default:*

```nix
null
```



*Example:*

```nix
"https://keycloak.my-project.com/realms/{realm-name}/.well-known/openid-configuration"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.mapping



Tells how to map claims to Buildbot userinfo



*Type:*
submodule



*Default:*

```nix
{
  email = "email";
  full_name = "name";
  groups = null;
  username = "preferred_username";
}
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.mapping\.email



Which claim to get the user’s email address from\.



*Type:*
string



*Default:*

```nix
"email"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.mapping\.full_name



Which claim to get the user’s full name from\.



*Type:*
string



*Default:*

```nix
"name"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.mapping\.groups



May be null if you do not want to provide groups\.



*Type:*
null or string



*Default:*

```nix
null
```



*Example:*

```nix
"groups"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.mapping\.username



Users are identified by this value



*Type:*
string



*Default:*

```nix
"preferred_username"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.name



User facing name of this provider



*Type:*
string



*Default:*

```nix
"OIDC Provider"
```



*Example:*

```nix
"Org PocketID"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.oidc\.scope



Scope that should be requested\. Note that options of type list are additive\.



*Type:*
list of string



*Default:*

```nix
[
  "openid"
  "email"
  "profile"
]
```



*Example:*

```nix
[
  "openid"
  "email"
  "profile"
  "groups"
]
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.outputsPath



Path where we store the latest build store paths names for nix attributes as text files\. This path will be exposed via nginx at ${domain}/nix-outputs



*Type:*
null or absolute path



*Default:*

```nix
null
```



*Example:*

```nix
"/var/www/buildbot/nix-outputs/"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.postBuildSteps



A list of steps to execute after every successful build\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```



*Example:*

```nix
[
  {
    name = "upload-to-s3";
    environment = {
      S3_TOKEN = "%(secret:s3-token)";
      S3_BUCKET = "bucket";
    };
    command = [ "nix" "copy" "%result%" ];
    warnOnly = true; # Don't fail the build if upload fails
  }
]

```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.postBuildSteps\.\*\.command



The command to execute as part of the build step\. Either a single string or
a list of strings\. Be careful that neither variant is interpreted by a shell,
but is passed to ` execve ` verbatim\. If you desire a shell, you must use
` writeShellScript ` or similar functions\.

To access the properties of a build, use the ` interpolate ` function defined in
` inputs.buildbot-nix.lib.interpolate ` like so ` (interpolate "result-%(prop:attr)s") `\.



*Type:*
string or list of (string or (A type representing a Buildbot interpolation string, supports interpolations like \`result-%(prop:attr)s\`\.
))

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.postBuildSteps\.\*\.environment



Extra environment variables to add to the environment of this build step\.
The base environment is the environment of the ` buildbot-worker ` service\.

To access the properties of a build, use the ` interpolate ` function defined in
` inputs.buildbot-nix.lib.interpolate ` like so ` (interpolate "result-%(prop:attr)s") `\.



*Type:*
attribute set of ((A type representing a Buildbot interpolation string, supports interpolations like \`result-%(prop:attr)s\`\.
) or string)



*Default:*

```nix
{ }
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.postBuildSteps\.\*\.name



The name of the build step, will show up in Buildbot’s UI\.



*Type:*
string

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.postBuildSteps\.\*\.warnOnly



If true, failures in this post-build step will be marked as warnings only
and will not cause the entire build to fail\. This is useful for optional
steps like uploading artifacts to external services or running non-critical
checks\.



*Type:*
boolean



*Default:*

```nix
false
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.pollInterval



How often to poll each repository by default expressed in seconds\. This value can be overridden
per repository\.



*Type:*
signed integer



*Default:*

```nix
60
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.pollSpread



If non-null and non-zero pulls will be randomly spread apart up to the specified
number of seconds\. Can be used to avoid a thundering herd situation\.



*Type:*
null or signed integer



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.repositories



Set of repositories that should be polled for changes\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{ }
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.repositories\.\<name>\.defaultBranch



The repositories default branch\.



*Type:*
string

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.repositories\.\<name>\.pollInterval



How often to poll this repository expressed in seconds\.



*Type:*
signed integer



*Default:*

```nix
60
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.repositories\.\<name>\.sshKnownHostsFile



If non-null the specified known hosts file will be matched against when connecting to
repositories over SSH\. This option defaults to the global ` sshKnownHostsFile ` option\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.repositories\.\<name>\.sshPrivateKeyFile



If non-null the specified SSH key will be used to fetch all configured repositories\.
This option is defaults to the global ` sshPrivateKeyFile ` option\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.repositories\.\<name>\.url



The repository’s URL, must be fetchable by git\.



*Type:*
string

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.sshKnownHostsFile



If non-null the specified known hosts file will be matched against when connecting to
repositories over SSH\. This option is overridden by the per-repository ` sshKnownHostsFile `
option\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.pullBased\.sshPrivateKeyFile



If non-null the specified SSH key will be used to fetch all configured repositories\.
This option is overridden by the per-repository ` sshPrivateKeyFile ` option\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.showTrace



Show stack traces on failed evaluations



*Type:*
null or boolean



*Default:*

```nix
false
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.useHTTPS



If buildbot is setup behind a reverse proxy other than the configured nginx set this to true
to force the endpoint to use https:// instead of http://\.



*Type:*
null or boolean



*Default:*

```nix
false
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.webhookBaseUrl



URL base for the webhook endpoint that will be registered for github or gitea repos\.



*Type:*
string



*Default:*

```nix
config.services.buildbot-master.buildbotUrl
```



*Example:*

```nix
"https://buildbot-webhooks.numtide.com/"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.master\.workersFile



File containing a list of nix workers



*Type:*
absolute path

*Declared by:*
 - [\<buildbot-nix/nixosModules/master\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/master.nix)



## services\.buildbot-nix\.worker\.enable



Whether to enable buildbot-worker\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/worker\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/worker.nix)



## services\.buildbot-nix\.worker\.masterUrl



The buildbot master url\.



*Type:*
string



*Default:*

```nix
"tcp:host=localhost:port=9989"
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/worker\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/worker.nix)



## services\.buildbot-nix\.worker\.name



The buildbot worker name\.



*Type:*
string



*Default:*

```nix
config.networking.hostName
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/worker\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/worker.nix)



## services\.buildbot-nix\.worker\.nixEvalJobs\.package



nix-eval-jobs to use for evaluation



*Type:*
package



*Default:*

```nix
<derivation nix-eval-jobs-2.33.0>
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/worker\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/worker.nix)



## services\.buildbot-nix\.worker\.workerPasswordFile



The buildbot worker password file\.



*Type:*
absolute path

*Declared by:*
 - [\<buildbot-nix/nixosModules/worker\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/worker.nix)



## services\.buildbot-nix\.worker\.workers

The number of workers to start (default: 0 == to the number of CPU cores)\.
If you experience flaky builds under high load, try to reduce this value\.



*Type:*
signed integer



*Default:*

```nix
0
```

*Declared by:*
 - [\<buildbot-nix/nixosModules/worker\.nix>](https://github.com/nix-community/buildbot-nix/blob/main/nixosModules/worker.nix)


