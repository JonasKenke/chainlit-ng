import type { Mock } from 'vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChainlitAPI, ClientError } from '../src/index';
import { mergePromptUpdates, promptErrorCode } from '../src/utils/prompt';

const api = new ChainlitAPI('http://app.test', 'webapp');

function respond(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockImplementation(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' }
      })
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function lastCall(fetchMock: Mock) {
  const call = fetchMock.mock.calls.at(-1);
  if (!call) throw new Error('fetch was not called');
  const [url, init] = call as [string, RequestInit];
  return {
    url,
    method: init.method,
    body: typeof init.body === 'string' ? JSON.parse(init.body) : init.body
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('prompt versioning API client', () => {
  it('lists owned versions newest-first payload unmodified', async () => {
    const versions = [{ id: 'v2', version: 2 }];
    const fetchMock = respond(versions);

    await expect(api.fetchPromptVersions('p1')).resolves.toEqual(versions);
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompts/p1/versions'
    );
    expect(lastCall(fetchMock).method).toBe('GET');
  });

  it('uses the anonymous share routes when shared is set', async () => {
    const fetchMock = respond([]);

    await api.fetchPromptVersions('shared-1', { shared: true });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompt/share/shared-1/versions'
    );

    await api.fetchPromptVersion('shared-1', 'v9', { shared: true });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompt/share/shared-1/versions/v9'
    );
  });

  it('fetches one owned version snapshot', async () => {
    const version = { id: 'v3', version: 3, content: 'body' };
    const fetchMock = respond(version);

    await expect(api.fetchPromptVersion('p1', 'v3')).resolves.toEqual(version);
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompts/p1/versions/v3'
    );
  });

  it('lists updates and acknowledges a source version', async () => {
    const fetchMock = respond([]);
    await api.fetchPromptUpdates();
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompts/updates'
    );

    fetchMock.mockImplementation(
      async () =>
        new Response(JSON.stringify({ promptId: 'p1', notifiedVersion: 4 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        })
    );
    await expect(api.ackPromptUpdates('p1', 4)).resolves.toEqual({
      promptId: 'p1',
      notifiedVersion: 4
    });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompts/updates/ack'
    );
    expect(lastCall(fetchMock).body).toEqual({
      promptId: 'p1',
      version: 4
    });
  });

  it('builds the diff query with from and encoded version id', async () => {
    const fetchMock = respond({ rows: [] });

    await api.fetchPromptDiff('p1', 'v 2', 'local');
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompts/p1/diff?from=local&to=v%202'
    );
  });

  it('omits absent sync fields and sends explicit ones', async () => {
    const fetchMock = respond({ id: 'p1' });

    await api.syncPrompt('p1', {
      versionId: 'v2',
      confirm: true,
      expectedLocalHash: 'sha256:abc'
    });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompts/p1/sync'
    );
    expect(lastCall(fetchMock).body).toEqual({
      versionId: 'v2',
      confirm: true,
      expectedLocalHash: 'sha256:abc'
    });

    await api.syncPrompt('p1', {});
    expect(lastCall(fetchMock).body).toEqual({});
  });

  it('detaches with an empty body', async () => {
    const fetchMock = respond({ id: 'p1' });

    await api.detachPrompt('p1');
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompts/p1/detach'
    );
    expect(lastCall(fetchMock).method).toBe('POST');
    expect(lastCall(fetchMock).body).toEqual({});
  });

  it('pins a version only when one is provided', async () => {
    const fetchMock = respond({ id: 'copy' });

    await api.addSharedPrompt('shared-1', 'v2');
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/prompt/share/shared-1/add'
    );
    expect(lastCall(fetchMock).body).toEqual({ versionId: 'v2' });

    await api.addSharedPrompt('shared-1');
    expect(lastCall(fetchMock).body).toEqual({});
  });

  it('surfaces the stable 409 code from a sync conflict', async () => {
    respond({ detail: { code: 'preview_stale', message: 'stale' } }, 409);

    const error = await api
      .syncPrompt('p1', { versionId: 'v2' })
      .catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ClientError);
    expect(promptErrorCode(error)).toBe('preview_stale');
  });

  it('returns undefined for errors without a structured code', () => {
    expect(promptErrorCode(new Error('boom'))).toBeUndefined();
    expect(promptErrorCode(undefined)).toBeUndefined();
    expect(promptErrorCode({ detail: 'plain' })).toBeUndefined();
  });
});

describe('mergePromptUpdates', () => {
  const notice = (promptId: string, latestVersion: number) => ({
    promptId,
    title: promptId,
    sourcePromptId: `source-${promptId}`,
    pinnedVersionId: null,
    pinnedVersion: null,
    localVersion: 1,
    latestVersionId: `v${latestVersion}`,
    latestVersion,
    notifiedVersion: 0
  });

  it('keeps a locally acknowledged notice that the server no longer lists', () => {
    const kept = { ...notice('p1', 3), notifiedVersion: 3 };
    expect(mergePromptUpdates([kept], [])).toEqual([kept]);
  });

  it('takes the newer notice and sorts by version', () => {
    const merged = mergePromptUpdates(
      [notice('p1', 2), { ...notice('p2', 5), notifiedVersion: 5 }],
      [notice('p1', 4)]
    );
    expect(
      merged.map((update) => [update.promptId, update.latestVersion])
    ).toEqual([
      ['p2', 5],
      ['p1', 4]
    ]);
  });

  it('ignores a stale server notice for a version already seen', () => {
    const merged = mergePromptUpdates([notice('p1', 4)], [notice('p1', 2)]);
    expect(merged[0].latestVersion).toBe(4);
  });
});

describe('ClientError', () => {
  it('renders structured conflict details as text, not JSON', () => {
    const error = new ClientError('Conflict', 409, {
      code: 'local_changes',
      message: 'This skill has local edits'
    });

    expect(error.toString()).toBe('Conflict: This skill has local edits');
    expect(error.toString()).not.toContain('{');
  });
});
