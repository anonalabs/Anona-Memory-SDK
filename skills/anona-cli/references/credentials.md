# Credentials

## The order

The data commands use the first that exists:

1. `--api-key KEY`
2. `ANONA_API_KEY` in the environment
3. `ANONA_API_KEY` in `~/.anona/config.env` (read as data; no need to source it)
4. the command-line login (`~/.anona/cli-credentials.json`, from `anona login`)
5. a temporary profile (`~/.anona/credentials.json`, from `anona start`)

An API key always outranks a stored login. The MCP login (also in
`~/.anona/credentials.json`, used by `anona mcp`) is never on this list; with
only that stored, the commands exit `4` and say to run `anona login --cli`.

`config.env` may hold only `ANONA_API_KEY`, `ANONA_SPACE_ID` and
`ANONA_BASE_URL`, and should be mode 600.

## Space

With 1-4, name a space: `--space S`, else `ANONA_SPACE_ID` (environment, then
`config.env`). With none, the command exits `2` and sends nothing. A temporary
profile's only space is `default`; omit `--space`.

Pick a space once per session: `ANONA_SPACE_ID` if set, else the repository
name lowercased, else `default`. `anona spaces list --json` shows what exists.

## Checking

```bash
anona status          # stored credentials, sources, expiry; local files only
anona doctor --json   # which wins and why, server health, space reachable, credits
```

`anona doctor` exits `0` when all is well, `4` for a credential problem, `7`
when the server is unreachable, `5` when the space does not exist, `2` when no
space is named, `6` when no credits are left. Neither command prints a key or token.

## Setting one up

| Situation | Do |
| --- | --- |
| The user has an API key | Ask them to export `ANONA_API_KEY` or put it in `~/.anona/config.env`. Never write a key into a file the repo tracks, and never echo it |
| The user can open a browser | Suggest `anona login --cli` in one line. Do not run it and wait: a person approves it |
| Nothing, and memory is wanted now | `anona start` (or `uvx --from anona anona start`). Tell the user it expires in 72 hours and `anona login` keeps it |
| CI | An API key from the CI secret store. Never `anona login` |

## Servers

A key from the environment or `config.env` is sent only to `api.anonalabs.com`
or `memory.anonalabs.com`, unless `ANONA_BASE_URL` sits in the same place. A
`--base-url` alone never redirects such a key (exit `4`,
`refusing to send the API key`); pass `--api-key` and `--base-url` together for
another server. A stored login only goes to the server that issued it; switch
with `anona login --base-url URL`.

## Named profiles

`--profile NAME` or `ANONA_PROFILE=NAME` uses a separate credential set and
`config.env` under `~/.anona/profiles/NAME/`. Use it to keep two accounts or two
servers apart. Names: letters, digits, `-`, `_`, at most 64.
