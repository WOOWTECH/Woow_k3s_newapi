# NA-05 dormant production proxy: review patch and activation blockers

## Boundary and exact parent

Prepared in a new isolated worktree `wt-na05-dormant-prod-1021`, branch
`feat/na05-dormant-prod-1021`, based on PR #18 head
`d487835aeb8c20ed8143e3d0de7ca94b7008e749`. PR #18 targets PR #17's branch;
PR #17 head inspected was `e020639`. Both were open/unmerged when checked.
This preparation makes no live query or deployment and reads no Secret/PAT.
I2/I3/LLM13/Singer/NA07/#17/#18 worktrees, aimodels and sandbox are not modified.

Committed production render remains **`proxy.enabled: false`**, no committed prod proxy image
pin, no proxy resources, no proxy Secret key/Valkey ACL and no new network paths.
Zero replicas is not a dormant substitute: enabling the component would still
alter Valkey/Secret and NetworkPolicies. The chart now rejects zero/negative/
fractional replicas and string booleans such as `enabled: "false"`.

## Scratch candidate review

`/data/pi-agent/tmp/na05-prod-render-1021.yaml` is an **active values override**,
not a harmless rendered manifest. It sets enabled=true, the stg-tested
3947d2b08d08 image/digest, registry Secret name, and candidate client selectors:
namespace `paas-system`, instance `paas-odoo-platform`, components server/cron.
It does not provide owner-created AP/OP/HMAC, actual production PAT expiry dates,
or current proof that these selectors match production. It was rendered offline
only. Do not commit it as the prod overlay or pass it to a live Helm upgrade.

An enabled render adds ConfigMap, Service, Deployment (two replicas), PDB and
proxy NetworkPolicy; it also adds a generated proxy Valkey password and changes
Valkey's ACL/config rollout. A render/green stg report is not permission to do that.

The existing CI check **requiring the prod proxy to remain absent is retained**.
The image contract gate is additionally fixed: every active production proxy must
have a matching green report even if an earlier dormant commit already held the
same pin. Digest-only pin changes no longer bypass the gate; production report
validation requires full commit-SHA tag and non-placeholder digest. Neither check
proves that production Secrets, permissions or network readiness are correct.

## Safest concrete GitOps PR path

1. **Preparation only:** review this patch against #18. Its stacked PR targets
   `feat/na07-stg-pin-sync-1021`, not main, so #17/#18 changes are not presented as
   this task's new diff. This PR does not authorize activation or deployment.
2. Owner reviews/merges **#17 -> main**, then retargets/reviews **#18 -> main**.
   After #18 lands, retarget/review the preparation branch against main. Keep the
   prod-off CI barrier, empty prod pin and enabled=false; this is safe code prep,
   not a deployment instruction. No change to backup policies or stg image pins.
3. **Owner prerequisite phase, outside this patch:** use the browser/approved
   access path to create `woow-paas-svc` role 10, channel permissions disabled,
   service-account 2FA; AP scopes log:read/user:read/user:write, OP option:read,
   approximately 180-day expiry. Create separate prod WEB/CRON HMAC material and
   registry pull credentials in `new-api`. Values contain only Secret names and
   expiry metadata. Owner alone handles the values; never put them in repo/logs.
   Coordinate the Odoo copies in `fleet-local/paas-odoo-fleet-secrets` under LLM-17,
   without editing aimodels or treating this infra prep as Odoo authorization.
4. **Separate activation PR only after prerequisites:** explicitly select the
   same full SHA/digest as an approved stg contract report (currently #18 carries
   rc.44 + 3947d2b08d08 evidence), add actual prod AP/OP expiry metadata, registry
   Secret reference and freshly confirmed exact Odoo selectors. Verify the
   report against these pins and review its date/version relevance. The existing
   production-off CI check must be intentionally replaced in this later PR;
   do not merely delete it here. Require documented owner prerequisite review,
   retained contract validation and an explicit deployment decision.
5. Owner separately authorizes/deploys the activation and completes NA-05 prod
   acceptance. Plan Valkey restart/ACL readiness and verify the copied rate-limit
   script remains loaded. Test source isolation, signed proxy health/readiness,
   AP/OP scope and expiry, generated replay ACL, and distinct prod/stg HMAC kids.
   `/readyz` alone cannot establish AP/OP scope correctness. LLM-17 and the GA
   enable switch are separate decisions; this patch changes no settings.

## Network constraints to retain

- Odoo connects only to the proxy, never directly to new-api. Do not add Odoo to
  `networkPolicy.clients`; enabling the proxy already adds its same-namespace
  selector to app ingress. The scratch Odoo labels are candidates until owner
  supplies current read-only production label evidence.
- Proxy ingress: namespace **AND** instance/component selectors; ports 8080/8081,
  plus existing Helm test pod. No blanket namespace or sandbox ingress in prod.
- Existing egress includes new-api:3000, **Valkey:6379** for shared replay, common
  DNS, and scoped client reply ports 32768-60999. The older ticket's shorter
  egress description must not be interpreted as deleting required Valkey access.
- Do not activate solely by applying the scratch file or scaling replicas to zero.
  Owner preflight must establish the named Secret/key existence without showing
  values; Helm template does not establish this, and this task does no lookup.

## Owner-only blockers before activation

- Prod admin/service-account 2FA, AP/OP scope tests, actual expiry metadata and
  separately managed production PAT/HMAC/registry Secrets have not been verified.
- Current prod Odoo instance/component labels and effective namespace policies
  have not been verified in this task (no live-resource access was requested).
- Existing stg report opens only the image contract gate; it cannot establish
  prod credential readiness, identity isolation or operational approval.
- Current CI intentionally rejects active prod. It must remain so until the
  explicit activation PR and deployment window are owner-approved.

## Local validation

Only offline Helm, script/unit and report checks are run. Tests include strict
boolean/positive replica guards, default/prod/stg render equivalence to #18 with
random offline Secret bytes redacted, and active-with-unchanged-pin contract
report validation, missing/mismatched/non-green evidence and digest-only refusal.
Exact commands/results and resource counts are recorded in this task's final
report; no inference, live acceptance, settings calls or Odoo scratch tests occur.


Validation results (2026-10-21):

| Check | Result |
|---|---|
| default / prod / stg Helm lint + template | Passed; 20 / 20 / 30 resources |
| Full normalized render vs #18 d487835 | All three identical, excluding random offline Secret bytes; key shape retained |
| Committed prod image pin | tag=None, digest=None; no active prod pin |
| Scratch candidate offline render | 25 resources, 2 proxy replicas; not committed or applied |
| NA-05 unit tests | 9 passed, including integer-overflow replica rejection |
| All scripts/tests | 110 passed, fake/in-process services only |
| Chart guards | 25 proxy cases passed; maintenance suite 14 tests, 1 real-age optional skip |
| Contract selfcheck | 10 passed, 48 IDs |
| Contract image gate against #18 / main | Passed; parent stg report matches exact pins |
| Existing rc.44 + 3947d2b08d08 report | gate open (image contract only, not prod authorization) |
| Python compile / bash syntax / diff whitespace | Passed |

Read-only Gitea PR metadata and local files only; no Kubernetes commands or
production/sandbox access were used. Scope is chart guard, explicit dormant prod
flag, contract gate, offline tests and review documents. No merge or live deployment.
