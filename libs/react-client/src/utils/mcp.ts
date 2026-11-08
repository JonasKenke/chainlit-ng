import { IMcp, IMcpConnectResponse } from '../types/mcp';

/**
 * Replace one entry by name. `map` never appends, so a late HTTP response or
 * socket event can never resurrect a server the user removed.
 */
const updateMcp = (mcps: IMcp[], name: string, patch: Partial<IMcp>): IMcp[] =>
  mcps.map((mcp) => (mcp.name === name ? { ...mcp, ...patch } : mcp));

/** `mcp_connected` / `200`: the session is attached. */
export const markMcpConnected = (mcps: IMcp[], mcp?: IMcp): IMcp[] =>
  mcp
    ? updateMcp(mcps, mcp.name, {
        status: 'connected',
        tools: mcp.tools,
        prompts: mcp.prompts ?? [],
        resources: mcp.resources ?? [],
        authorizationUrl: undefined,
        detail: undefined
      })
    : mcps;

/** `mcp_authorization_required` / `202`: the user still has to sign in. */
export const markMcpAuthorizationRequired = (
  mcps: IMcp[],
  name: string,
  authorizationUrl?: string
): IMcp[] =>
  updateMcp(mcps, name, {
    status: 'connecting',
    authorizationUrl,
    detail: undefined
  });

/** `mcp_connection_failed` / `400` / `429`: keep the backend detail verbatim. */
export const markMcpFailed = (
  mcps: IMcp[],
  name: string,
  detail?: string
): IMcp[] =>
  updateMcp(mcps, name, {
    status: 'failed',
    detail,
    authorizationUrl: undefined
  });

/** Apply a `POST /mcp` body; a `202` is a pending sign-in, not a failure. */
export const applyMcpConnectResponse = (
  mcps: IMcp[],
  name: string,
  response: IMcpConnectResponse
): IMcp[] =>
  response?.status === 'authorization_required'
    ? markMcpAuthorizationRequired(mcps, name, response.url)
    : markMcpConnected(mcps, response?.mcp);
