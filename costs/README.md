# Cost profiles

Cost profiles are published inputs to the platform's transparent cost estimates. Copy
`profiles/a10-node.template.json` to `profiles/<name>.json`, replace every placeholder,
and provide the positive all-inclusive server price in `nodeHourlyRateUsd`.

v0.1 accepts USD whole-node pricing only. The rate is assigned to GPUs as
`nodeHourlyRateUsd / physicalGpuCount`; CPU, RAM, storage, and network prices remain zero
to avoid counting one dedicated-node bill more than once. Electricity is reported as
energy efficiency but is not priced separately because `powerIncludedInRate` must be true.

Profiles are intentionally public and are embedded, with their canonical SHA-256 hash and
pricing source, in benchmark reports. Never add billing credentials or private invoices.
