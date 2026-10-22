# NA-07 backup image publisher

New workflow: `.gitea/workflows/build-newapi-backup.yml`, following
`odoo-addons/woow_paas_platform/.gitea/workflows/build-newapi-proxy.yml`.
This sidecar only adds that workflow and this document. No image pins, values,
Dockerfile, maintenance scripts, settings or cluster resources are changed.
Parent `d0ceabf`'s stg legacy backup **keepDays=7** correction is preserved.

## Publication boundary

- No PR/tag/schedule/arbitrary-input trigger. Main pushes touching this workflow
  or `images/backup/Dockerfile` can publish after merge. Manual dispatch accepts
  only main or `feat/na-07-retention-backup` in `woow-paas/Woow_k3s_newapi`.
  Adding this workflow does **not** dispatch a run.
- Checkout must equal the 40-hex event SHA and contain current origin/main.
  Stale/divergent manual bases fail before registry credentials are supplied.
- The entire reviewed Dockerfile is locked by SHA-256
  `7cd86ded6ce1a5a0505ece29279b1c076d00511b4f80fe5f531380797981c9ce`,
  including its pinned PostgreSQL 16.15/Alpine base. Recipe changes require a
  reviewed workflow hash update. Extra tracked image files fail; the actual
  temporary build context contains **only** a copied Dockerfile, not the checkout,
  backups or registry config. No build args, SSH keys or private identities.
- Fixed internal destination:
  `artifactory-jcr-artifactory-nginx.jfrog.svc.cluster.local/woow-paas-docker-local/newapi-backup:<SHA>`.
  Outputs use the existing `jcr-prod.woowtech.io` alias for the same JCR repository.
  No mutable tag is pushed. Existing SHA tags are pulled, smoke-tested and reused
  without rebuilding/pushing. Only explicit manifest-missing errors allow build;
  auth/network/unrecognized errors fail closed. A second check before push rejects
  tags appearing during build; same-SHA runs serialize. Absolute atomic protection
  against unrelated writers still requires JCR-side tag immutability; this policy
  was not inspected or changed. Other publishers must not overwrite SHA tags.
- Candidate smoke uses linux/amd64, uid/gid 70, read-only root FS, no network or
  capabilities and writable `/tmp`; checks Bash, age, pg_dump, pg_restore, df/find.
  It does not generate/use a private key or connect to any database.
- After push/reuse, pull the tag, select its exact repository RepoDigest, reject
  malformed/all-zero digests, pull **by digest**, and check OCI revision against
  the event SHA. Job outputs/console/optional summary record `image`, `digest`,
  `pinned`. No automatic change to `encryptedBackup.image` or deployment occurs.

## Secrets and runner inspection

Existing secret names: **`JCR_USERNAME` / `JCR_PASSWORD`**, matching the PaaS
publisher. They enter only the login step's environment and `--password-stdin`,
not shell source or image layers. Per-run 0700 `DOCKER_CONFIG` is outside checkout
and removed by `always()` cleanup. The mirror's SSH secret is never referenced.
Credentials need read/push permission for only the backup image path, with SHA
rewrite protection. Never print/copy credential values for setup verification.

Read-only evidence inspected on 2026-10-21:

| Evidence | Observation |
|---|---|
| PaaS run 24503, build job 26798 | Successful build-newapi-proxy on develop; login/build/push/digest steps succeeded |
| Build runner | `act-runner-runner-0`, label `ubuntu-latest` |
| Infra PR #17 CI run 24534, job 26831 | Successful on the same runner/label |
| Local runner manifest snapshot (2026-10-10) | Gitea runner 2.0.1 + privileged Docker 28.5.2 DinD sidecar; historical, not a live probe |
| Infra repo secret-name API | Only `GH_MIRROR_SSH_KEY` listed; JCR names absent at repo scope |
| Org secrets / org and global runner APIs | HTTP 403; cannot confirm inherited credentials/current shared runner configuration |
| Repo runner API | Zero repo-specific registrations; successful CI demonstrates shared-runner use |

Before first approved run, ensure both JCR secret names are available **to this
repo**, through inherited scope or repo secrets, and authorize the backup image
path. Repo listing alone cannot prove inherited secrets are missing: PaaS also
lists no repo secrets despite its successful publisher. Workflow fails explicitly
if either secret resolves empty; this sidecar creates no secrets or permissions.

Required runner capabilities: Bash, git/coreutils, Docker CLI plus reachable
linux/x86_64 daemon, public base-image/APK network access, internal JCR DNS and the
same daemon HTTP/insecure-registry or trusted TLS setup as the existing publisher.
Preflight checks CLI/daemon/OS/architecture. Actual registry transport/auth and
new repository write scope remain unproven until an approved run. No local Docker
daemon is available here; no runner job was launched and no image was built.

## Local checks

- actionlint 1.7.7 **with ShellCheck 0.10.0** passed.
- YAML parse and `bash -n` on all eight inline run blocks passed.
- **20 offline static/stub checks passed**, executing actual workflow shell blocks
  against mock Docker/git in temporary directories: repo/event/ref/SHA/base/recipe/
  context/architecture rejection; missing secrets before login; auth/network/unknown
  registry fail-closed; existing-tag reuse without build/push; isolated build
  context; pre-push tag race rejection; invalid digest/repository/revision rejection;
  digest pull, output recording and credential cleanup. No real Docker/registry
  calls occurred. The container smoke body's commands were syntax-checked only.
- `git diff --check` and only-new-workflow/document scope check passed.

Tools and stub harness are under `/tmp/na07-workflow-check`; none enters this repo.
These checks prove syntax/control flow, not successful image build, registry
permissions, current runner health or restore acceptance. No workflow dispatch,
publication, push, PR, merge, staging/prod mutation, image pin or settings change.
