# NA-05 dormant production proxy preparation

> Execute locally in the new worktree based on PR #18 head d487835. No live access, credentials, active prod image pin, push or PR publication.

**Goal:** Prepare a safe GitOps review path while production proxy stays absent.

**Architecture:** Explicit production enabled=false; strict chart boolean/positive replica guards; existing production-off CI retained; contract gate validates active production even without a pin change. Owner-only Secret/identity/expiry and deployment checks remain separate prerequisites.

**Tech Stack:** Helm v3, stdlib unittest, existing contract report validator.

1. Reproduce the scratch candidate as offline render only; inventory its effects and verify the existing report without copying an active prod pin into git.
2. Add chart guard tests for string booleans and invalid replica counts, strict boolean/positive replicas checks, explicit dormant production override.
3. Add contract gate tests for first activation/re-enabling unchanged pin and digest-only pin; require full commit-SHA/digest and green matching stg report.
4. Document the PR chain (#17 -> #18 -> new prep branch), barriers to later activation, owner Secret/AP/OP/HMAC/labels/expiry work and Valkey rollout effect. No deployment instructions are executed.
5. Verify default/prod resource equivalence against #18 (excluding offline random Secret bytes), unchanged stg render, chart guards, unit tests, contract selfcheck/gate; commit locally and provide review patch/blockers.
