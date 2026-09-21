# Parent/child workflow composition

Demonstrates a Runtime `workflow` node dispatching an exact child name and version. Runtime stores
the child independently, preserves parent/root/correlation lineage, returns the child's canonical
output to the parent, and reconciles the recorded child during recovery rather than redispatching it.

Run `python -m examples.pattern_child_workflow.app` or import this folder into Studio. The example is
credential-free and deterministic. Production parent nodes should remain `recovery_policy: fail`;
Runtime reconciles an already-created child checkpoint regardless of that policy.
