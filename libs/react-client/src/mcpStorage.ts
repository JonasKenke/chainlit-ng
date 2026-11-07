import { IMcp } from './types/mcp';

export const MCP_STORAGE_KEY = 'mcp_storage_key';

const MCP_TYPES = ['sse', 'stdio', 'streamable-http'] as const;
const USER_TYPES = ['sse', 'streamable-http'] as const;
const STATUSES = ['connected', 'connecting', 'failed'] as const;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const asNonEmptyString = (value: unknown): string | undefined =>
  typeof value === 'string' && value ? value : undefined;

/**
 * Strip credentials from a URL before it is persisted or displayed. The raw
 * URL stays in memory only, so userinfo/query tokens are never at rest.
 *
 * shortcut: all query params are dropped, so query-authenticated servers must
 * be re-entered after reload.
 */
export function sanitizeMcpUrl(url: string | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    parsed.username = '';
    parsed.password = '';
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString();
  } catch {
    return undefined;
  }
}

function parseStatus(value: unknown): IMcp['status'] {
  return STATUSES.includes(value as (typeof STATUSES)[number])
    ? (value as IMcp['status'])
    : 'failed';
}

/**
 * Parse the persisted MCP list, migrating entries written before named servers
 * existed. Headers and tools never survive a reload; URLs are sanitized.
 * Returns an empty list for corrupt or non-array payloads; never throws.
 */
export function parseStoredMcps(raw: string | null): IMcp[] {
  if (!raw) return [];

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];

  const mcps: IMcp[] = [];
  const seen = new Set<string>();
  for (const value of parsed) {
    if (!isRecord(value)) continue;

    const name = asNonEmptyString(value.name);
    if (!name || seen.has(name)) continue;

    const status = parseStatus(value.status);
    const clientType = value.clientType;

    if (clientType !== undefined) {
      if (clientType === 'stdio') {
        // Pre-port stdio entries carried a client-supplied command, which the
        // server now rejects; reconnect them by name against config instead.
        mcps.push({ name, tools: [], status, type: 'stdio' });
        seen.add(name);
        continue;
      }

      const url = sanitizeMcpUrl(asNonEmptyString(value.url));
      if (
        !USER_TYPES.includes(clientType as (typeof USER_TYPES)[number]) ||
        !url
      ) {
        continue;
      }
      mcps.push({
        name,
        tools: [],
        status,
        clientType: clientType as IMcp['clientType'],
        isUserProvided: true,
        url
      });
      seen.add(name);
      continue;
    }

    if (!MCP_TYPES.includes(value.type as (typeof MCP_TYPES)[number])) continue;
    mcps.push({
      name,
      tools: [],
      status,
      type: value.type as IMcp['type']
    });
    seen.add(name);
  }

  return mcps;
}

/**
 * Serialize the in-memory list without credentials. Headers, tools and
 * credential-bearing URL parts must never reach localStorage.
 */
export function serializeStoredMcps(mcps: IMcp[]): string {
  const seen = new Set<string>();
  const stored = mcps.flatMap((mcp) => {
    if (seen.has(mcp.name)) return [];
    seen.add(mcp.name);

    const url = sanitizeMcpUrl(mcp.url);
    if (mcp.isUserProvided && (!mcp.clientType || !url)) return [];

    const entry: Record<string, unknown> = {
      name: mcp.name,
      status: mcp.status
    };
    if (mcp.type) entry.type = mcp.type;
    if (mcp.isUserProvided) entry.isUserProvided = true;
    if (mcp.clientType) entry.clientType = mcp.clientType;
    if (url) entry.url = url;
    return [entry];
  });

  return JSON.stringify(stored);
}
