# NA-05 initial production proxy activation

Production currently has no `newapi-proxy`. This change enables two replicas of
the exact image already running in staging, with ingress restricted to the
`paas-system` namespace AND `paas-odoo-platform` instance AND `server|cron`
components. It deliberately replaces the prior production-off CI assertion with
rendered component/image/identity checks while retaining the contract report gate.

## Verified prerequisites — 2026-10-25

- Production root and `woow-paas-svc` have 2FA enabled. Service role 10, group
  default, channel read/operate/write all false, verified through authenticated
  readback. New setup sessions were revoked. Recovery material is only in the
  owner's private Mac credential directory, outside this repository.
- Service-owned AP has exactly `log:read,user:read,user:write`; root-owned OP has
  exactly `option:read`. Both expire at Unix `1808450171` (180 days at creation).
  Each token was created through its own browser-session authentication protocol
  and TOTP step-up, persisted privately, and bound to the subject's own PAT list.
- Six real GET scope checks passed: AP user-search 200, channel/option 403; OP
  option 200, user-search/channel 403. The older ticket's same-role admin-detail
  GET is not claimed: rc.44's target-role guard rejects that operation. No
  `profile:read` expansion was made; #22–24 remain separate future work.
- `new-api/newapi-proxy-pats` UID `e7a9768b-700c-4ad0-8871-f38a49a3b194`,
  AP/OP paired in RAM after `kubectl create`; no last-applied annotation.
- `new-api/newapi-proxy-hmac` UID `ae64157e-62d7-4ae8-a4cd-5faeb7cd0b78`,
  paired with the dormant Odoo Secret in the earlier prerequisite run.
- `new-api/newapi-proxy-registry` UID `af630727-958a-43e2-9bbc-3fdad3878d34`,
  created from the current production pull identity for only
  `jcr-prod.woowtech.io`. An authenticated HTTPS HEAD returned 200 and the exact
  proxy digest. No staging pull credential was copied.
- Fresh production web/cron pod labels match the proposed namespace/instance/
  component selector. Odoo currently runs `.258`; its AI features stay disabled.
- Production display repair executed exactly two PUTs, with readback and repeat
  no-op; all other options unchanged. Independent runtime invariants: 11/11.

## Exact image contract

Proxy `3947d2b08d0813a2fae9f120e8fa67990f86c4cb` at
`sha256:0fdf5e12da8c8fec9d4b1650d2a134d6dc58d1ba86b82d54ea62b8b6fb8ce3bd`.
New-api remains `v1.0.0-rc.44` at
`sha256:67bf271602177ff31bbb4b34f20d5f33003d3108514bd23412cff66498c05a84`.
The matching committed full staging gate report is
`tests/contract/reports/v1.0.0-rc.44_3947d2b08d08.json`, produced by the existing
`impl/contract-run/gate.sh` workflow. Validate the report and activation gate
against these exact pins. This uses the existing full gate for an unchanged
staging image pair; no fresh disruptive phase is claimed or required by this PR.

## Merge and deployment sequence

1. Elmo reviews and merges this activation PR. Agents do not merge or approve PRs.
2. Recheck production main, the three Secret UIDs/keys, actual PAT expiries,
   paired HMAC keys, Odoo pod labels and the matching gate report. Announce the
   deployment window: enabling the component adds the Valkey replay ACL and can
   restart Valkey. Preserve existing Helm-generated credentials using the
   established release upgrade procedure; never apply a newly rendered Secret.
3. Deploy the reviewed chart/values with bounded health/rollout monitoring.
   Confirm the exact image, two Ready replicas, PDB and Valkey replay ACL/script.
4. From actual Odoo cron, verify signed health/readiness and AP/OP remaining days.
   Verify rejected unsigned/wrong-signature calls, and packet-level denial from
   a `paas-system` pod without Odoo labels and from a `paas-ws-*` namespace.
   Verify Odoo cannot reach new-api:3000 directly. Preserve the existing scoped
   DNS, upstream, shared Valkey and reply paths. Record test cleanup.
5. On failure, return to the prior disabled proxy values and verify new-api/
   Valkey health; keep AP/OP and HMAC material for diagnosis. Do not blindly
   revoke or regenerate them, and do not call `bootstrap --rotate`.

## Separate remaining launch work

Fleet #332 remains a draft pending confirmation of two independent offline
Fernet recovery copies. Cloudflare staging management-only Access is configured,
but WAF/organization GET returned 403; relay bypass/tunnel/DNS and staging then
production exposure tests remain. Literal final terms inputs and personal consent,
then real owner provisioning/Qwen/credits/usage/key-deletion acceptance, remain.
This proxy deployment is an internal prerequisite, not a public AI launch.
