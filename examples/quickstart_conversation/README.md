# Conversation quickstart

A two-turn conversation through `ConversationService` and the built-in `runtime` handler
(`RuntimeConversationHandler`), printed as the event stream a chat UI would render. It runs offline
on the mock provider; no credentials or services are needed:

```bash
python -m examples.quickstart_conversation.app            # approve the address change
python -m examples.quickstart_conversation.app --reject   # reject it
```

| What | API | Where you see it |
| --- | --- | --- |
| Streamed answer | `conversation_streaming` in `agent.yaml`; the mock provider streams word by word | `assistant.message.delta` conversation events, printed after `assistant>` |
| A note for the UI | `await context.emit_note(title=..., suggestions=..., data=...)` in `orders.track` | a `tool.note` run event; the model never sees it |
| Steering mid-turn | `await runtime.steer(run_id, tenant_id, text)` while `orders.track` runs | `steer.delivered`; the next model call ends with the message |
| Approval pause and resume | `orders.change_address` is a `write()` tool, so the agent's approval policy asks first; `ConversationService.respond(..., ConversationApprovalDecision(decision="approved"))` | `assistant.message.awaiting_approval`, the message's `pending` (tool, proposed action, redacted parameters), then `assistant.message.resumed` and `completed` |

```text
tools.py    the two typed tools (@tool): orders.track sends a note, orders.change_address is a write
plugin.py   OrdersPlugin, which registers them
agent.yaml  the mock provider, the order-assistant agent and its plugin, conversation_streaming
app.py      the transcript printer and the scripted conversation
```

A sample transcript (the run id differs):

```text
user> Where is my order A-100?
  [assistant] started
  [tool] orders.track running
  [tool note] orders.track: Checked the carrier for A-100
              suggestions: Change the delivery address
user (steering run 16773da5)> Also tell me the delivery window, please.
  [steering] 1 message(s) delivered to the model
assistant> Order A-100 is in transit with Parcelforce and arrives on Friday, in the 09:00 to 13:00 delivery window.
  [assistant] completed
  [model] its next call ended with the steering message: 'Also tell me the delivery window, please.'

user> Please deliver it to 12 Harbour Road, Leith instead.
  [assistant] started
  [assistant] waiting for approval
  [approval] orders.change_address: Execute orders.change_address
             parameters: {'order_id': 'A-100', 'address': '12 Harbour Road, Leith'}
user> (approved)
  [assistant] resumed: approved by example-user
  [tool] orders.change_address running
  [tool note] orders.change_address: Delivery address of A-100 changed
assistant> Done: order A-100 will be delivered to 12 Harbour Road, Leith.
  [assistant] completed
```

Notes:

- The model is scripted with `MockModelProvider.queue`, so its words do not depend on the steering
  message; the transcript shows that the message reached the model's next call. With a real
  provider, drop the queue and the model decides.
- `orders.track` waits on a gate until the steering message is queued, only to make the demo
  deterministic. Without it a message sent at any time is delivered at the run's next step
  boundary, or comes back as `RunResult.undelivered_steering` if the run ends first.
- `delta_interval_ms: 0` sends every chunk as its own delta event. The default (50 ms) coalesces
  small chunks; see [Conversation turns](../../docs/operations.md#conversation-turns-streaming-approvals-and-input).
- A rejected change fails the turn (`approval_policy.on_rejection: fail_run`, the default). Set
  `on_rejection: continue` to let the agent answer after a rejection.
- Over HTTP the same flow is `POST /v1/conversations/{id}/messages`,
  `GET /v1/conversations/{id}/events` (SSE), `POST /v1/runs/{run_id}/steer` and
  `POST /v1/conversations/{id}/turns/{turn_id}/approval`. See
  [Steering a running run](../../docs/operations.md#steering-a-running-run) and
  [Notes for the user interface](../../docs/tool-authoring.md#notes-for-the-user-interface).
