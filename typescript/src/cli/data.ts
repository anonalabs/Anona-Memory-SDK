/**
 * The data commands' entry point: the usage checks that send nothing, the
 * credential, then memory.ts or verbs.ts. Mirrors the record/retrieve/reason
 * and resource-verb half of anona/cli/main.py.
 */
import { loadConfigEnv } from "./config-env.js";
import { findApiKey, findBaseUrl, findSpace, redact } from "./credential.js";
import {
  AUTH_EXIT,
  readBatch,
  readContent,
  runReason,
  runRecord,
  runRecordBatch,
  runRetrieve,
  USAGE_EXIT,
  type DataIO,
} from "./memory.js";
import { printable } from "./py.js";
import { cliCredentialsPath, isSignedIn, loadCredentials } from "./store.js";
import { forApiKey, resolveTarget, TargetError } from "./target.js";
import { DEFAULT_WAIT, needsSpace, precheck, runVerb, type Verb } from "./verbs.js";

/** Sanitized like server text: the URL in a refusal came from a flag or a variable. */
function refuseKey(io: DataIO, e: TargetError): number {
  io.stderr.write(`anona: ${printable(redact(e.message))}\n`);
  return AUTH_EXIT;
}

function guard<T>(io: DataIO, fn: () => T): T | number {
  try {
    return fn();
  } catch (e) {
    if (e instanceof TargetError) return refuseKey(io, e);
    throw e;
  }
}

/**
 * [base URL, space, API key or undefined] for a data verb, or the exit code.
 *
 * Local only: config.env, the environment and the stored files are read, and
 * nothing is sent. With an API key the host is bound by forApiKey and, when the
 * verb addresses a space, one must be named; the command-line login needs one
 * too; without either the stored temporary profile's deployment is used and
 * its space defaults later, once the token is read.
 */
export function dataCredential(
  io: DataIO,
  args: Record<string, unknown>,
  baseUrlFlag: string | undefined,
  requested: string | undefined,
  needSpace: boolean,
): [string, string | undefined, string | undefined] | number {
  const config = loadConfigEnv(io.stderr);
  const key = findApiKey(args.api_key as string | undefined, config);
  let space = args.space as string | undefined;
  if (key === null) {
    const cli = loadCredentials(cliCredentialsPath());
    if (cli !== null && isSignedIn(cli)) {
      // Pinned to the deployment that issued it, like every stored credential.
      const base = guard(io, () => resolveTarget(requested, cli.baseUrl));
      if (typeof base === "number") return base;
      if (needSpace) {
        // The same rule as an API key: an account can hold many spaces and
        // the server implies none. `default` is only a temporary profile's.
        space = findSpace(space, config);
        if (space === undefined) {
          io.stderr.write(
            "anona: name a space. Pass --space, or set ANONA_SPACE_ID " +
              "(in the environment or ~/.anona/config.env); `anona spaces list` shows yours. Nothing was sent.\n",
          );
          return USAGE_EXIT;
        }
      }
      return [base, space, undefined];
    }
    const held = loadCredentials();
    const base = guard(io, () => resolveTarget(requested, held?.baseUrl));
    if (typeof base === "number") return base;
    return [base, space, undefined];
  }
  if (needSpace) {
    // Picking a space for the caller is how a write lands somewhere nobody looks.
    space = findSpace(space, config);
    if (space === undefined) {
      io.stderr.write(
        "anona: an API key needs a space. Pass --space, or set ANONA_SPACE_ID " +
          "(in the environment or ~/.anona/config.env). Nothing was sent.\n",
      );
      return USAGE_EXIT;
    }
  }
  const [url, urlSource] = findBaseUrl(baseUrlFlag, config);
  const base = guard(io, () => forApiKey(url, urlSource, key.source));
  if (typeof base === "number") return base;
  return [base, space, key.key];
}

/** record's flag combinations that cannot work: exit 2, nothing sent. */
function recordUsage(io: DataIO, args: Record<string, unknown>): number | null {
  const no = (message: string) => {
    io.stderr.write(`anona: ${message} Nothing was sent.\n`);
    return USAGE_EXIT;
  };
  if (args.batch !== undefined) {
    if (args.content !== undefined || args.file !== undefined) return no("give --batch, or the content / --file, not both.");
    if (args.background) return no("--batch is always queued; drop --async.");
    const named = (
      [
        ["--tags", args.tags],
        ["--timestamp", args.timestamp],
        ["--metadata", args.metadata],
      ] as const
    )
      .filter(([, v]) => v !== undefined)
      .map(([f]) => f);
    if (named.length) return no(`${named.join(", ")} cannot be combined with --batch; put them on each line of the file.`);
  }
  if (args.wait && !(args.background || args.batch !== undefined)) return no("--wait needs --async or --batch; a plain record already waits.");
  if (args.wait_timeout !== undefined && !args.wait) return no("--wait-timeout needs --wait.");
  return null;
}

export async function runData(
  io: DataIO,
  verb: Verb,
  args: Record<string, unknown>,
  baseUrl: string | undefined,
  requested: string | undefined,
): Promise<number> {
  const [command] = verb;
  const scope = {
    user_id: args.user_id as string | undefined,
    agent_id: args.agent_id as string | undefined,
    session_id: args.session_id as string | undefined,
  };

  if (command === "record" || command === "retrieve" || command === "reason") {
    // Usage first: a command that is wrong exits 2 before a credential is
    // looked up, and so before anything could be sent.
    let content = "";
    let items: Record<string, unknown>[] = [];
    if (command === "record") {
      const bad = recordUsage(io, args);
      if (bad !== null) return bad;
      if (args.batch !== undefined) {
        const got = await readBatch(io, args.batch as string);
        if (typeof got === "number") return got;
        items = got;
      } else {
        const got = await readContent(io, args.content as string | undefined, args.file as string | undefined);
        if (typeof got === "number") return got;
        content = got;
      }
    }
    if (command === "retrieve" && args.tags_match !== undefined && args.tags === undefined) {
      io.stderr.write("anona: --tags-match needs --tags; there is nothing for it to match. Nothing was sent.\n");
      return USAGE_EXIT;
    }
    if (command === "retrieve" && args.max_tokens !== undefined && args.format !== "block") {
      io.stderr.write("anona: --max-tokens needs --format block. Nothing was sent.\n");
      return USAGE_EXIT;
    }
    const got = dataCredential(io, args, baseUrl, requested, true);
    if (typeof got === "number") return got;
    const [base, space, apiKey] = got;
    const flat = (lists: unknown) => (lists === undefined ? undefined : (lists as string[][]).flat());
    const asJson = Boolean(args.json);
    if (command === "record") {
      const wait = args.wait ? ((args.wait_timeout as number | undefined) ?? DEFAULT_WAIT) : undefined;
      if (args.batch !== undefined) return runRecordBatch(io, base, items, space, { apiKey, asJson, wait, ...scope });
      return runRecord(io, base, content, space, {
        apiKey,
        asJson,
        tags: flat(args.tags),
        timestamp: args.timestamp as string | undefined,
        metadata: args.metadata as Record<string, unknown> | undefined,
        background: Boolean(args.background),
        wait,
        ...scope,
      });
    }
    if (command === "reason") {
      return runReason(io, base, args.query as string, space, { apiKey, asJson, depth: args.depth as string | undefined, ...scope });
    }
    return runRetrieve(io, base, args.query as string, space, {
      apiKey,
      asJson,
      limit: args.limit as number | undefined,
      minScore: args.min_score as number | undefined,
      tags: flat(args.tags),
      tagsMatch: args.tags_match as string | undefined,
      asOf: args.as_of as string | undefined,
      occurredAfter: args.occurred_after as string | undefined,
      occurredBefore: args.occurred_before as string | undefined,
      tagGroups: args.tag_groups as unknown[] | undefined,
      memberId: args.member_id as string | undefined,
      preferObservations: args.prefer_observations as boolean | undefined,
      block: args.format === "block",
      maxTokens: args.max_tokens as number | undefined,
      ...scope,
    });
  }

  const bad = precheck(io, verb, args);
  if (bad !== null) return bad;
  const got = dataCredential(io, args, baseUrl, requested, needsSpace(verb));
  if (typeof got === "number") return got;
  const [base, space, apiKey] = got;
  return runVerb(io, verb, args, base, space, apiKey);
}
