# IT incident response

A synthetic multi-agent workflow:

```text
classify -> [logs || metrics] -> join -> analyze -> remediation plan -> checkpoint -> execute -> report
```

Run `python -m examples.reference_incident_response.app` or import this folder into Studio. The
execution handler never invokes a shell or real infrastructure API; it records only a simulated,
idempotent outcome. Production use requires authenticated telemetry adapters, durable checkpoints,
role-separated approval, and a reviewed remediation allowlist.
