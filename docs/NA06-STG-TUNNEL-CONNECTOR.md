# NA-06 staging Cloudflare tunnel connector

This staging-only change enables the chart-managed `newapi-cloudflared` Deployment in namespace `new-api-stg`. It consumes the already-created `newapi-cloudflared` Secret (`cloudflared.tokenSecret`) and connects to the remotely managed dormant tunnel `woow-k3s-new-api-stg`.

Main's `impl/prod-controlled-test-1024/staging-tunnel-create-result.json` (checked 2026-10-24 01:09:50 UTC) records successful creation of tunnel `woow-k3s-new-api-stg` (`dc92ef6e-89c1-47bc-81ed-52b6ec029bc3`), verified closed ingress, no origin exposure, and `dns_changed=false`. It records Secret verification at `new-api-stg/newapi-cloudflared`; this is the connector-token Secret consumed by the chart, not an owner identity or future owner-input field. No Secret value is included here. This change adds neither a hostname route nor DNS. A healthy connector only proves connection to the tunnel; it does not create an origin route or make the hostname reachable. The chart NetworkPolicy conditionally adds the `newapi-cloudflared` policy: egress is limited to the same-release new-api relay service on TCP/3000 and Cloudflare edge on UDP/TCP 7844 and TCP/443; ingress is denied by policy. Existing proxy client selectors, proxy image, new-api images, and other chart settings are unchanged.

## Release and gates

- Source is `deploy/woow-k3s-stg.yaml`; release remains `newapi` in `new-api-stg`. The production values file is not part of this change.
- The existing staging deployment process applies the values file. The Secret must already exist under the configured name; this PR does not create or inspect it.
- Keep DNS absent while Cloudflare Access and the zone custom-WAF entrypoint are unverified. Before publication, follow [`NA06-CLOUDFLARE-PREFLIGHT.md`](NA06-CLOUDFLARE-PREFLIGHT.md): establish the exact-host management Access app and owner-only policy, install/read back the exact-host WAF guard before any relay Bypass app, then pass the GET-only preflight. Current operator-reported rulesets GET `403 [10000]` means WAF is not verified and DNS publication is blocked.
- Only after Access and WAF are verified may the staging hostname route be added to the tunnel and the single proxied CNAME be created. Do not point the hostname at an origin or add a broad/wildcard route as part of this connector change.
- After rollout, verify the connector Deployment becomes ready and the remote tunnel reports connected. Separately verify DNS remains absent and remote ingress remains only the 404 fallback until the Access/WAF gates pass. Connector health alone is not public-exposure acceptance.

## Local validation

Render the staging chart with and without the `cloudflared.enabled` change and compare resources. Expected delta is only the `newapi-cloudflared` Deployment and `newapi-cloudflared` NetworkPolicy; the existing proxy PodDisruptionBudget and all other resources must be identical. No cluster command is part of this validation.
