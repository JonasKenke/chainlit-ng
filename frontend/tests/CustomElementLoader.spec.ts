import { describe, expect, it, vi } from 'vitest';

import {
  CustomElementApiClient,
  loadCustomElement
} from '@/components/Elements/CustomElement/loader';

const baseImports = {
  react: { useState: () => null }
};

const entry = '/public/elements/entry.jsx';

function createApiClient(files: Record<string, string>) {
  const get = vi.fn(async (path: string): Promise<Response> => {
    const source = files[path];
    if (source === undefined) {
      throw new Error(`404 Not Found: ${path}`);
    }
    return { text: async () => source } as Response;
  });
  return { get };
}

function load(
  files: Record<string, string>,
  name = 'entry',
  apiClient: CustomElementApiClient = createApiClient(files)
) {
  return loadCustomElement({ name, imports: baseImports, apiClient });
}

describe('custom element loader', () => {
  it('resolves default, named, namespace, and side-effect imports', async () => {
    const files = {
      '/public/elements/demo/entry.jsx': `
        import Helper, { named } from './helper.jsx';
        import * as helper from './helper.jsx';
        import './side-effect.jsx';
        import lib from '../lib/helper.jsx';

        export default function Entry() {
          return [Helper, named, helper, lib];
        }
      `,
      '/public/elements/demo/helper.jsx': `
        export default function Helper() {
          return null;
        }
        export const named = 42;
      `,
      '/public/elements/demo/side-effect.jsx': `
        export const loaded = true;
      `,
      '/public/elements/lib/helper.jsx': `
        export default function LibHelper() {
          return null;
        }
      `
    };

    const { code, imports } = await load(files, 'demo/entry');

    expect(code).toContain("from '/public/elements/demo/helper.jsx'");
    expect(code).toContain("import '/public/elements/demo/side-effect.jsx'");
    expect(code).toContain("from '/public/elements/lib/helper.jsx'");

    expect(imports['/public/elements/demo/helper.jsx']).toMatchObject({
      named: 42
    });
    expect(imports['/public/elements/demo/side-effect.jsx']).toMatchObject({
      loaded: true
    });
    expect(imports['/public/elements/lib/helper.jsx']).toHaveProperty(
      'default'
    );
    expect(imports['/public/elements/demo/entry.jsx']).toHaveProperty(
      'default'
    );
  });

  it('lets a helper import another helper and an allowlisted package', async () => {
    const files = {
      [entry]: `
        import Outer from './outer.jsx';
        export default Outer;
      `,
      '/public/elements/outer.jsx': `
        import Inner from './inner.jsx';
        import { useState } from 'react';

        export default function Outer() {
          return [Inner, useState];
        }
      `,
      '/public/elements/inner.jsx': `
        export default function Inner() {
          return null;
        }
      `
    };

    const { imports } = await load(files);

    expect(imports['/public/elements/inner.jsx']).toHaveProperty('default');
    expect(imports['/public/elements/outer.jsx']).toHaveProperty('default');
  });

  it('passes known bare specifiers through unchanged', async () => {
    const files = {
      [entry]: `
        import { useState } from 'react';
        export default useState;
      `
    };
    const apiClient = createApiClient(files);

    const { code, imports } = await load(files, 'entry', apiClient);

    expect(code).toContain("from 'react'");
    expect(imports.react).toBe(baseImports.react);
    expect(apiClient.get).toHaveBeenCalledTimes(1);
  });

  it('rejects unknown bare specifiers', async () => {
    const files = {
      [entry]: `import axios from 'axios';`
    };

    await expect(load(files)).rejects.toThrow(
      "Module not found: 'axios' (imported by /public/elements/entry.jsx)"
    );
  });

  it.each([
    ['https://evil.example/helper.jsx', /remote URLs are not allowed/],
    ['http://evil.example/helper.jsx', /remote URLs are not allowed/],
    ['//evil.example/helper.jsx', /protocol-relative imports are not allowed/],
    ['/public/elements/helper.jsx', /absolute paths are not allowed/],
    ['../../secret.jsx', /path escapes \/public\/elements\//],
    ['./nested/../../secret.jsx', /path escapes \/public\/elements\//],
    ['./helper.jsx?v=1', /query strings and fragments are not supported/],
    ['./helper.jsx#fragment', /query strings and fragments are not supported/]
  ])('rejects %s without fetching it', async (specifier, message) => {
    const apiClient = createApiClient({
      [entry]: `import value from '${specifier}';`
    });

    await expect(
      load({ [entry]: `import value from '${specifier}';` }, 'entry', apiClient)
    ).rejects.toThrow(message);
    expect(apiClient.get).toHaveBeenCalledTimes(1);
    expect(apiClient.get).toHaveBeenCalledWith(entry);
    for (const [path] of apiClient.get.mock.calls) {
      expect(path.startsWith('/public/elements/')).toBe(true);
    }
  });

  it('rejects an entry name that escapes the elements directory', async () => {
    const apiClient = createApiClient({});

    await expect(load({}, '../../secret', apiClient)).rejects.toThrow(
      /entry must stay under \/public\/elements\//
    );
    expect(apiClient.get).not.toHaveBeenCalled();
  });

  it('reports a missing entry file', async () => {
    const apiClient = createApiClient({});

    await expect(load({}, 'ghost', apiClient)).rejects.toThrow(
      "Failed to load '/public/elements/ghost.jsx': 404 Not Found: /public/elements/ghost.jsx"
    );
  });

  it('reports a missing helper file', async () => {
    const files = {
      [entry]: `import missing from './missing.jsx';`
    };
    const apiClient = createApiClient(files);

    await expect(load(files, 'entry', apiClient)).rejects.toThrow(
      "Failed to load '/public/elements/missing.jsx'"
    );
    expect(apiClient.get).toHaveBeenCalledWith('/public/elements/missing.jsx');
  });

  it('detects cycles and includes the full chain', async () => {
    const files = {
      [entry]: `import a from './a.jsx';`,
      '/public/elements/a.jsx': `import b from './b.jsx';`,
      '/public/elements/b.jsx': `import a from './a.jsx';`
    };
    const apiClient = createApiClient(files);

    await expect(load(files, 'entry', apiClient)).rejects.toThrow(
      /Circular import detected:.*entry\.jsx.*a\.jsx.*b\.jsx.*a\.jsx/
    );
    expect(apiClient.get).toHaveBeenCalledTimes(3);
  });

  it('stops at the import depth limit', async () => {
    const files: Record<string, string> = {
      [entry]: `import module from './m1.jsx';`
    };
    for (let i = 1; i <= 33; i += 1) {
      const next = i === 33 ? '' : `import module from './m${i + 1}.jsx';`;
      files[`/public/elements/m${i}.jsx`] = `${next}
        export default ${i};`;
    }
    const apiClient = createApiClient(files);

    await expect(load(files, 'entry', apiClient)).rejects.toThrow(
      /Import depth limit of 32 exceeded:.*m33\.jsx/
    );
    const paths = apiClient.get.mock.calls.map(([path]) => path);
    expect(paths).not.toContain('/public/elements/m33.jsx');
    expect(paths).toHaveLength(33);
  });

  it('fetches a shared helper once per graph', async () => {
    const shared = '/public/elements/shared.jsx';
    const files = {
      [entry]: `
        import shared from './shared.jsx';
        import a from './a.jsx';
        export default [shared, a];
      `,
      '/public/elements/a.jsx': `
        import shared from '../elements/shared.jsx';
        export default shared;
      `,
      [shared]: `export default 1;`
    };
    const apiClient = createApiClient(files);

    const { code } = await load(files, 'entry', apiClient);

    expect(code).toContain("from '/public/elements/shared.jsx'");
    const sharedCalls = apiClient.get.mock.calls.filter(
      ([path]) => path === shared
    );
    expect(sharedCalls).toHaveLength(1);
    expect(apiClient.get).toHaveBeenCalledTimes(3);
  });
});
