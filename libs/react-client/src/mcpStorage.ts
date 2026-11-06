import { IMcp } from './types/mcp';

export const MCP_STORAGE_KEY = 'mcp_storage_key';

const MCP_TYPES = ['sse', 'stdio', 'streamable-http'] as const;
const USER_TYPES = ['sse', 'streamable-http'] as const;
const STATUSES = ['connected', 'connecting', 'failed'] as const;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const asNonEmptyString = (value: unknown): string | undefined =>
  typeof value === 'string' && value ? value : undefined;

function parseTools(value: unknown): IMcp['tools'] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((tool) => isRecord(tool) && asNonEmptyString(tool.name))
    .map((tool) => ({ name: tool.name as string })) as IMcp['tools'];
}

function parseHeaders(value: unknown): Record<string, string> | undefined {
  if (!isRecord(value)) return undefined;
  const entries = Object.entries(value).filter(
    (entry): entry is [string, string] => typeof entry[1] === 'string'
  );
  return entries.length ? Object.fromEntries(entries) : undefined;
}

/**
 * Parse the persisted MCP list, migrating entries written before named servers
 * existed. Returns an empty list for corrupt or non-array payloads; never throws.
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
  for (const value of parsed) {
    if (!isRecord(value)) continue;

    const name = asNonEmptyString(value.name);
    if (!name) continue;

    const status = STATUSES.includes(value.status as (typeof STATUSES)[number])
      ? (value.status as IMcp['status'])
      : 'failed';
    const tools = parseTools(value.tools);
    const clientType = value.clientType;

    if (clientType !== undefined) {
      if (clientType === 'stdio') {
        // Pre-port stdio entries carried a client-supplied command, which the
        // server now rejects; reconnect them by name against config instead.
        mcps.push({ name, tools, status, type: 'stdio' });
        continue;
      }

      const url = asNonEmptyString(value.url);
      if (
        !USER_TYPES.includes(clientType as (typeof USER_TYPES)[number]) ||
        !url
      ) {
        continue;
      }
      const headers = parseHeaders(value.headers);
      mcps.push({
        name,
        tools,
        status,
        clientType: clientType as IMcp['clientType'],
        isUserProvided: true,
        url,
        ...(headers ? { headers } : {})
      });
      continue;
    }

    if (!MCP_TYPES.includes(value.type as (typeof MCP_TYPES)[number])) continue;
    mcps.push({ name, tools, status, type: value.type as IMcp['type'] });
  }

  return mcps;
}
