"""NA-03 contract tests for new-api-stg and the staging newapi-proxy.

Every behaviour the PaaS "AI 模型" tab relies on is pinned here as an
automated test, so a new-api upgrade (or a proxy change) must go green on
new-api-stg before the production PR may merge (docs/OPERATIONS.md 〈升版〉).

Test ids come straight from the contracts:

* C01-C27 — 〔PROV〕notes/I2-provisioning-contract.md §6.3 / DESIGN §8.3.
* F01-F12 — 〔FREE〕notes/I1-free-tier-limits.md §7 / DESIGN §8.3.
* X01-X09 — proxy contract, DESIGN §8.3 / extra/newapi-proxy/README.md.

Pure standard library plus pytest. Nothing here imports new-api or Odoo; the
offline self-checks (``selfcheck.py`` / ``test_offline.py``) run in CI with no
cluster, the C/F/X tests skip unless their target is configured (``config.py``).
"""
