import { importCode } from 'react-runner';

/**
 * Loads a custom element and its relative helper imports.
 *
 * Supported grammar: static, top-level `import` declarations with an explicit
 * `.jsx` extension (default, named, namespace, and side-effect imports).
 * Not supported: dynamic `import()`, import cycles, imports outside
 * `/public/elements/`, percent-encoded paths, and bare packages outside the
 * caller's base import map.
 *
 * The entry module is only rewritten, never compiled: `Runner` evaluates it
 * with its runtime globals (`apiClient`, `props`, ...) in scope.
 */

const ELEMENTS_ROOT = '/public/elements/';
const LOADER_ORIGIN = 'https://chainlit.invalid';
const MAX_IMPORT_DEPTH = 32;

// Matches `import <clause> from '<specifier>'` and `import '<specifier>'`.
const IMPORT_DECLARATION =
  /\bimport\s+(?:[\w$*{},\s]+?\s+from\s+)?(['"])([^'"]+)\1/g;

export type ImportMap = Record<string, unknown>;

export interface CustomElementApiClient {
  get(endpoint: string): Promise<Response>;
}

export interface LoadCustomElementOptions {
  /** Element name used by the backend, e.g. `Counter` or `nested/Counter`. */
  name: string;
  /** Existing runner imports (UI kit, `@/components/renderer`, packages). */
  imports: ImportMap;
  apiClient: CustomElementApiClient;
}

export interface LoadedCustomElement {
  /** Entry source with relative specifiers rewritten to canonical paths. */
  code: string;
  /** Base imports plus every compiled helper keyed by canonical path. */
  imports: ImportMap;
}

export async function loadCustomElement({
  name,
  imports: baseImports,
  apiClient
}: LoadCustomElementOptions): Promise<LoadedCustomElement> {
  const entryPath = resolveEntryPath(name);
  const registry: ImportMap = { ...baseImports };
  const rewritten = new Map<string, string>();
  const sources = new Map<string, string>();
  const stack: string[] = [];

  /** Rewrites a module's imports; compiles it unless it is the entry. */
  const load = async (
    path: string,
    depth: number,
    isEntry: boolean
  ): Promise<string> => {
    const cached = rewritten.get(path);
    if (cached !== undefined) return cached;

    const cycleStart = stack.indexOf(path);
    if (cycleStart !== -1) {
      throw new Error(
        `Circular import detected: ${[...stack, path].join(' -> ')}`
      );
    }

    if (depth > MAX_IMPORT_DEPTH) {
      throw new Error(
        `Import depth limit of ${MAX_IMPORT_DEPTH} exceeded: ${[...stack, path].join(' -> ')}`
      );
    }

    stack.push(path);
    try {
      const source = sources.get(path) ?? (await fetchSource(apiClient, path));
      sources.set(path, source);

      const rewrites = new Map<string, string>();
      for (const specifier of collectSpecifiers(source)) {
        const dependency = resolveSpecifier(specifier, path, baseImports);
        if (dependency === undefined) continue;
        await load(dependency, depth + 1, false);
        rewrites.set(specifier, dependency);
      }

      const code = rewriteImports(source, rewrites);
      if (!isEntry) {
        try {
          registry[path] = importCode(code, { import: registry });
        } catch (error) {
          throw new Error(
            `Failed to compile '${path}': ${errorMessage(error)}`,
            { cause: error }
          );
        }
      }
      rewritten.set(path, code);
      return code;
    } finally {
      stack.pop();
    }
  };

  const code = await load(entryPath, 0, true);
  return { code, imports: registry };
}

function resolveEntryPath(name: string): string {
  if (/[%?#]/.test(name)) {
    throw new Error(
      `Invalid custom element name '${name}': must not contain '?', '#', or percent-encoding`
    );
  }

  const url = new URL(`${name}.jsx`, LOADER_ORIGIN + ELEMENTS_ROOT);
  if (url.origin !== LOADER_ORIGIN || !url.pathname.startsWith(ELEMENTS_ROOT)) {
    throw new Error(
      `Invalid custom element name '${name}': entry must stay under ${ELEMENTS_ROOT}`
    );
  }
  return url.pathname;
}

/**
 * Returns the canonical path for a relative import, or `undefined` when the
 * specifier is a known bare import that should pass through unchanged.
 */
function resolveSpecifier(
  specifier: string,
  importer: string,
  baseImports: ImportMap
): string | undefined {
  // Reject all percent-encoding: the server decodes it once more, so `%2f`,
  // `%5c`, and encoded dot segments could escape `/public/elements/`.
  if (specifier.includes('%')) {
    throw invalidImport(
      specifier,
      importer,
      'percent-encoding is not supported'
    );
  }

  if (isRelative(specifier)) {
    if (/[?#]/.test(specifier)) {
      throw invalidImport(
        specifier,
        importer,
        'query strings and fragments are not supported'
      );
    }
    if (specifier.includes('\\')) {
      throw invalidImport(specifier, importer, 'backslashes are not supported');
    }

    const url = new URL(specifier, LOADER_ORIGIN + importer);
    if (
      url.origin !== LOADER_ORIGIN ||
      !url.pathname.startsWith(ELEMENTS_ROOT)
    ) {
      throw invalidImport(specifier, importer, `path escapes ${ELEMENTS_ROOT}`);
    }
    return url.pathname;
  }

  if (specifier.startsWith('//')) {
    throw invalidImport(
      specifier,
      importer,
      'protocol-relative imports are not allowed'
    );
  }
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(specifier)) {
    throw invalidImport(specifier, importer, 'remote URLs are not allowed');
  }
  if (specifier.startsWith('/')) {
    throw invalidImport(specifier, importer, 'absolute paths are not allowed');
  }

  if (!Object.prototype.hasOwnProperty.call(baseImports, specifier)) {
    throw new Error(
      `Module not found: '${specifier}' (imported by ${importer})`
    );
  }
  return undefined;
}

function isRelative(specifier: string): boolean {
  return (
    specifier === '.' ||
    specifier === '..' ||
    specifier.startsWith('./') ||
    specifier.startsWith('../')
  );
}

function invalidImport(
  specifier: string,
  importer: string,
  reason: string
): Error {
  return new Error(`Invalid import '${specifier}' in '${importer}': ${reason}`);
}

function collectSpecifiers(source: string): string[] {
  const specifiers = new Set<string>();
  for (const match of source.matchAll(IMPORT_DECLARATION)) {
    specifiers.add(match[2]);
  }
  return [...specifiers];
}

function rewriteImports(source: string, rewrites: Map<string, string>): string {
  return source.replace(
    IMPORT_DECLARATION,
    (declaration, quote: string, specifier: string) => {
      const resolved = rewrites.get(specifier);
      if (resolved === undefined) return declaration;
      const prefix = declaration.slice(
        0,
        declaration.length - specifier.length - 2
      );
      return `${prefix}${quote}${resolved}${quote}`;
    }
  );
}

async function fetchSource(
  apiClient: CustomElementApiClient,
  path: string
): Promise<string> {
  try {
    const response = await apiClient.get(path);
    return await response.text();
  } catch (error) {
    throw new Error(`Failed to load '${path}': ${errorMessage(error)}`, {
      cause: error
    });
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
