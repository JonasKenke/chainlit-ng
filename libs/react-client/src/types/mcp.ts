export interface IMcpPromptArgument {
  name: string;
  description?: string | null;
  required?: boolean;
}

/** A prompt published by a connected MCP server. */
export interface IMcpPrompt {
  name: string;
  description?: string | null;
  arguments?: IMcpPromptArgument[];
}

/** A resource published by a connected MCP server. */
export interface IMcpResource {
  uri: string;
  name: string;
  description?: string | null;
  mimeType?: string | null;
  size?: number | null;
}

export interface IMcp {
  name: string;
  tools: { name: string }[];
  prompts?: IMcpPrompt[];
  resources?: IMcpResource[];
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
  /** Opt in to the per-user OAuth flow (streamable HTTP only). */
  useOAuth?: boolean;
  /** Authorization URL awaiting sign-in; in-memory only, never persisted. */
  authorizationUrl?: string;
  /** Backend failure detail, verbatim; in-memory only, never persisted. */
  detail?: string;
}

/** Body of `POST /mcp`; a `202` carries `status` + `url` instead of `success`. */
export interface IMcpConnectResponse {
  success?: boolean;
  mcp?: IMcp;
  status?: 'authorization_required';
  url?: string;
}

/** Body of `POST /mcp/prompt`: the rendered prompt text for the composer. */
export interface IMcpPromptResponse {
  text: string;
}

/** Body of `POST /mcp/resource`: the element shown in the chat. */
export interface IMcpResourceResponse {
  element: Record<string, unknown>;
}
