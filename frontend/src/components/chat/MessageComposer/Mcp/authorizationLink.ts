export interface AuthorizationLink {
  href: string;
  host: string;
}

/**
 * A backend-supplied authorization URL is only opened if it is a plain
 * `http(s)` URL. `javascript:` and `data:` are refused, and the host is
 * returned so the UI can show who is asking for the sign-in.
 */
export const getAuthorizationLink = (
  url?: string
): AuthorizationLink | null => {
  if (!url) return null;

  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return null;
  }
  if (!parsed.host) return null;

  return { href: parsed.href, host: parsed.host };
};

/** Only call from a click handler; returns false for a refused URL. */
export const openAuthorizationUrl = (url?: string): boolean => {
  const link = getAuthorizationLink(url);
  if (!link) return false;

  window.open(link.href, '_blank', 'noopener,noreferrer');
  return true;
};
