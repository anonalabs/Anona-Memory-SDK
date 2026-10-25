"""The command tree, read off the argparse parser itself.

`anona guide` and `anona completion` are both generated from this, so neither
can list a flag that does not exist or miss one that does. argparse has no
public API for walking a parser; `_actions` and `_SubParsersAction` have been
stable since Python 3.2 and are what every completion generator reads.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field


@dataclass
class Option:
    flags: list[str]
    help: str
    takes_value: bool
    metavar: str | None = None
    choices: list[str] | None = None
    required: bool = False


@dataclass
class Positional:
    name: str
    help: str
    many: bool = False
    optional: bool = False
    choices: list[str] | None = None


@dataclass
class Command:
    path: list[str]
    help: str
    options: list[Option] = field(default_factory=list)
    positionals: list[Positional] = field(default_factory=list)
    children: list[Command] = field(default_factory=list)

    @property
    def name(self) -> str:
        return " ".join(self.path)

    def leaves(self) -> list[Command]:
        if not self.children:
            return [self]
        out: list[Command] = []
        for child in self.children:
            out.extend(child.leaves())
        return out

    def walk(self) -> list[Command]:
        out: list[Command] = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


def _help(text: str | None) -> str:
    if not text or text == argparse.SUPPRESS:
        return ""
    return " ".join(text.split())


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def global_flags(parser: argparse.ArgumentParser) -> set[str]:
    """The flags every command accepts (the shared parent's), by option string."""
    flags: set[str] = set()
    for action in parser._actions:
        if action.option_strings and not isinstance(action, (argparse._HelpAction, argparse._VersionAction)):
            flags.update(action.option_strings)
    return flags


def _options(parser: argparse.ArgumentParser, skip: set[str]) -> tuple[list[Option], list[Positional]]:
    options: list[Option] = []
    positionals: list[Positional] = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.option_strings:
            if skip.intersection(action.option_strings):
                continue
            options.append(
                Option(
                    flags=list(action.option_strings),
                    help=_help(action.help),
                    takes_value=action.nargs != 0,
                    metavar=(action.metavar if isinstance(action.metavar, str) else None)
                    or (action.dest.upper() if action.nargs != 0 else None),
                    choices=[str(c) for c in action.choices] if action.choices else None,
                    required=bool(action.required),
                )
            )
        else:
            positionals.append(
                Positional(
                    name=(action.metavar if isinstance(action.metavar, str) else None) or action.dest.upper(),
                    help=_help(action.help),
                    many=action.nargs in ("+", "*"),
                    optional=action.nargs in ("?", "*"),
                    choices=[str(c) for c in action.choices] if action.choices else None,
                )
            )
    return options, positionals


def tree(parser: argparse.ArgumentParser) -> Command:
    """The whole command tree. Global flags appear on the root only."""
    skip = global_flags(parser)
    root_options, _ = _options(parser, set())
    root = Command(path=["anona"], help=_help(parser.description), options=root_options)

    def build(p: argparse.ArgumentParser, path: list[str], help_text: str) -> Command:
        options, positionals = _options(p, skip)
        cmd = Command(path=path, help=help_text, options=options, positionals=positionals)
        sub = _subparsers(p)
        if sub is not None:
            helps = {a.dest: _help(a.help) for a in sub._choices_actions}
            for name, child in sub.choices.items():
                cmd.children.append(build(child, path + [name], helps.get(name, "")))
        return cmd

    sub = _subparsers(parser)
    assert sub is not None
    helps = {a.dest: _help(a.help) for a in sub._choices_actions}
    for name, child in sub.choices.items():
        root.children.append(build(child, [name], helps.get(name, "")))
    return root


def usage(cmd: Command) -> str:
    parts = ["anona", *cmd.path]
    for pos in cmd.positionals:
        token = pos.name + ("..." if pos.many else "")
        parts.append(f"[{token}]" if pos.optional else token)
    for opt in cmd.options:
        if opt.required:
            parts.append(f"{opt.flags[0]} {opt.metavar}" if opt.takes_value else opt.flags[0])
    if any(not o.required for o in cmd.options):
        parts.append("[options]")
    return " ".join(parts)


def as_dict(cmd: Command) -> dict:
    return {
        "name": cmd.name,
        "path": cmd.path,
        "help": cmd.help,
        "positionals": [p.__dict__ for p in cmd.positionals],
        "options": [o.__dict__ for o in cmd.options],
        "subcommands": [as_dict(c) for c in cmd.children],
    }
