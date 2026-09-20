# quickstart_approval

**Tier:** Quickstart | **Status:** 🚧 Placeholder — not yet implemented

> [!NOTE]
> This example will show one visible pause, a human decision (approve or reject), and a resume or abort path.

## Planned scope

- One agent with one write-side-effect tool requiring approval.
- Demonstrates the full approval lifecycle: propose → pause → approve/reject → resume/abort.
- Uses `approval_policy.require_for_side_effects: true`.
- Approval decision via CLI prompt (no UI required at quickstart tier).
- One smoke test covering both approve and reject paths without paid credentials.

## Implementation checklist

- [ ] Create `agent.yaml` with `approval_policy`.
- [ ] Create `app.py` with an interactive CLI loop for the approval decision.
- [ ] Create `manifest.yaml`.
- [ ] Create `tests/test_smoke.py`.
- [ ] Update this README with setup/run/test/reset instructions and expected output.
