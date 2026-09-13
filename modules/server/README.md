# server — design notes

## the agent ties it all together

The owner doesn't paste URLs into third-party dashboards themselves. Sample conversation:

```
owner:  wire up my toaster to track usage
agent:  your IoT endpoint is https://<api-id>.execute-api.us-east-1.amazonaws.com/iot/toaster
        and your signing secret is <generated>. paste both into your toaster's webhook config.
        i'll also tag electric usage as your UTILITIES_EXPENSE account going forward
        (already in your chart) — confirm?

owner:  yep

agent:  done. once you've pasted the URL i'll watch for the first event and confirm receipt.
```

Under the hood the agent generated a secret, wrote it to SSM (via `collect_secret`), looked up the customer's api endpoint (from SSM tenant metadata populated at provisioning), and composed the natural-language instructions. Same pattern for "wire stripe to accounting", "wire hotschedules to labor", "wire ramp to purchasing", etc. The agent is the front-end; the owner module owns the route.

## why a registry, not a hardcoded enum

Every source could hardcode the account names it emits — and would, in a salesforce-shaped product. The DDB-backed per-customer registry instead lets:

- the customer's agent extend the chart on owner demand
- the operator agent watch agreement across customers and promote new entries to canonical
- new customers inherit the latest canonical at provisioning
- existing customers pull canonical updates weekly via cron with owner approval

The cost is that every source lambda reads from the customer's registry rather than a baked-in enum; the validator (`is_account` in `post_journal_entry`) is the safety net for source code out of sync with the customer's tenancy.
