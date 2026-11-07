export interface IMcp {
  name: string;
  tools: { name: string }[];
  status: 'connected' | 'connecting' | 'failed';
  /** Transport of a server declared in `[[features.mcp.servers]]`. */
  type?: 'sse' | 'stdio' | 'streamable-http';
  /** True when the URL/headers were supplied by the user, not by config. */
  isUserProvided?: boolean;
  /** Transport of a user-provided server; SSE/HTTP only. */
  clientType?: 'sse' | 'streamable-http';
  /** Raw URL; sanitize before persisting or displaying. */
  url?: string;
  /** In-memory only; never persisted or replayed after reload. */
  headers?: Record<string, string>;
}
