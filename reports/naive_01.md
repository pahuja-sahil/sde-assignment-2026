# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.340 | -0.6% | 0.242 | -1.1% | 0.496 | +0.8% | 0% | 52 (57) |
| nw_kyc | 0.396 | +8.0% | 0.245 | +1.2% | 0.634 | +6.1% | 19% | 20 (21) |
| nw_renewal | 0.412 | -1.0% | 0.260 | -1.7% | 0.548 | +1.1% | 0% | 41 (54) |
| ALL | 0.376 | +1.0% | 0.249 | -0.9% | 0.541 | +2.1% | 4% | 113 (132) |

## Changes applied by the controller

5 applied — neutral: 3, beneficial: 2

- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['voice'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=-0.003 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 14,
  "decisions": {
    "approved": 5,
    "rejected": 1,
    "expired": 7
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 5,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 1,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0