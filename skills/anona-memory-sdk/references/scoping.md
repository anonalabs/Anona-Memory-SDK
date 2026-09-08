# Scoping one space to many users

Three optional keys, on every write and every read: `user_id`, `agent_id`,
`session_id`. They are how one space serves many end users without their
memories mixing.

```python
client.record("support-bot", "Prefers email over phone.", user_id="acme-42")
client.retrieve("support-bot", "contact preference", user_id="acme-42")
```

```ts
await anona.record({ spaceId: "support-bot", content: "Prefers email over phone.", userId: "acme-42" });
await anona.retrieve({ spaceId: "support-bot", query: "contact preference", userId: "acme-42" });
```

## The rule that surprises people

**The match is strict in both directions.** A scoped read never returns an
unscoped memory, and an unscoped read never returns a scoped one.

That is the isolation guarantee, and it has one consequence worth planning for:
a space that adopts scoping partway through its life stops seeing everything
written before it, on every scoped query, until those memories are rewritten.
Decide the convention when you create the space.

An unscoped call is byte-identical to what it was before scoping existed, so
adding the feature costs a space that does not use it nothing.

## Which key

- `user_id` - the end user. Almost always the one you want.
- `agent_id` - which agent in a multi-agent system wrote it.
- `session_id` - one conversation. Narrow; use it for scratch context, not for
  anything the next session should still know.

Synthesis is scoped per user, falling back to agent then session. Per-session
synthesis would fragment into thin per-conversation answers that never learn
across sessions.

## Tags

`tags` on a write are visibility-scope tags that `retrieve` can filter on. They
are for dimensions the three scope keys do not cover, such as the source system.

`anona:` is a **reserved prefix**. A tag starting with it is rejected with
`422 reserved_tag`, because the platform stamps its own tags there and a
customer-supplied one would be forgeable.

## Every write surface needs scope

File upload takes `user_id` / `agent_id` / `session_id` as **form fields**, not
JSON. A document uploaded without them is invisible to every scoped query, and
nothing warns you: the upload succeeds, is charged, and the content simply never
comes back.

The same applies to anything you build on top. A write path that does not carry
scope is either unreachable from scoped reads or, if it takes the scope from an
untrusted request body, forgeable.

## Never derive a user id from something guessable

`user_id` is a label your backend supplies about your own end users. Inside one
organization that is correct: your backend is the only caller. Do not expose it
to an end user's own input, or one user can file a write as another and read
another's scope.
