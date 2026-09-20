# Governed research pattern

A credential-free evidence workflow:

```text
bounded evidence collection -> cited synthesis -> deterministic citation verification
```

Run `python -m examples.pattern_governed_research.app` or import `agent.yaml` into Studio. Evidence is
synthetic and allowlisted. A production application should replace the collection handler with a
tenant-authorized retrieval adapter and retain the verifier boundary.
