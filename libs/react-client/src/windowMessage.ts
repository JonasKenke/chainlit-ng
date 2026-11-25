/**
 * Origin checks for the window-message bridge.
 *
 * A wildcard CORS entry is never treated as a trusted peer: `*` is not an
 * origin and cannot be compared to `MessageEvent.origin`.
 */
export function isTrustedMessageOrigin(
  origin: string,
  currentOrigin: string,
  allowedOrigins: readonly string[] = []
): boolean {
  if (!origin) return false;
  if (origin === currentOrigin) return true;
  return allowedOrigins.some(
    (allowed) => allowed !== '*' && allowed === origin
  );
}

/** Best-effort origin of the embedding page, without trusting the sender. */
export function getParentWindowOrigin(): string | null {
  try {
    if (document.referrer) {
      return new URL(document.referrer).origin;
    }
  } catch {
    // A malformed referrer is not a usable origin.
  }
  const location = window.location as unknown as {
    ancestorOrigins?: ArrayLike<string>;
  };
  const ancestors = location.ancestorOrigins;
  return ancestors && ancestors.length > 0 ? ancestors[0] : null;
}
