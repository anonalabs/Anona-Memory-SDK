<!-- Thanks for contributing. Delete any section that does not apply. -->

## What changed and why

<!-- The behaviour, and the failure it fixes. If it is a bug, say what a user
     saw before this. -->

## Checklist

- [ ] **Tests cover the behaviour that changed** — and fail without the fix.
      A test that passes either way documents; it does not protect.
- [ ] **Version bumped, if this changes shipped code.** Three places, and they
      must agree:
      `anona/__init__.py` (`__version__`), `pyproject.toml` (`version`), and
      `typescript/src` changes in `typescript/package.json`.
      This repo publishes to PyPI and npm, so code without a bump ships to
      nobody.
- [ ] **Python and TypeScript stay in step** where the change affects both —
      a field added to one client and not the other is a silent gap for half
      the users.
- [ ] `examples/` still run if the surface they use moved.

## Maintainers

- [ ] **Synced upstream.** This package is developed alongside an internal
      copy. A fix that lands here and is not carried back gets quietly
      reverted the next time the two are reconciled — it has happened, and it
      cost a released fix five days of being absent from the source everyone
      else develops against.
