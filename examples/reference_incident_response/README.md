# IT incident response

A synthetic multi-agent workflow:

```text
classify -> [logs || metrics] -> join -> analyze -> remediation plan -> approval -> execute -> report
```

### Running interactively

```bash
python -m examples.reference_incident_response.app
```

### Running non-interactively / automation

```bash
# Automatically approve remediation
python -m examples.reference_incident_response.app --approve-all

# Automatically reject remediation
python -m examples.reference_incident_response.app --reject-all

# Provide decision via answers file
python -m examples.reference_incident_response.app --answers-file path/to/answers.json
```

Or import this folder into Studio.

The execution handler never invokes a shell or real infrastructure API; it records only a simulated,
idempotent outcome. Runtime's first-class approval node retains the reviewer decision before the
synthetic write. Production use requires authenticated telemetry adapters, durable checkpoints,
role-separated authorization, and a reviewed remediation allowlist.
