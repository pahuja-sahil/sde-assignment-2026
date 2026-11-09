# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.346 | +1.2% | 0.243 | -0.8% | 0.501 | +1.9% | -2% | 62 (57) |
| nw_kyc | 0.382 | +4.1% | 0.240 | -0.7% | 0.624 | +4.5% | 32% | 22 (21) |
| nw_renewal | 0.422 | +1.3% | 0.262 | -0.8% | 0.554 | +2.3% | 14% | 51 (54) |
| ALL | 0.379 | +1.8% | 0.249 | -0.8% | 0.543 | +2.6% | 12% | 135 (132) |

## Changes applied by the controller

3 applied — beneficial: 2, neutral: 1

- nw_kyc v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice'] engageΔ=+0.074 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 3,
  "decisions": {
    "approved": 3,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 3,
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
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.258,
    "4": 0.239,
    "5": 0.255,
    "6": 0.254,
    "7": 0.24
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0