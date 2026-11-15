import type { Mock } from 'vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ISkillUpdateNotice } from '../src/types/skill';

import { ChainlitAPI, ClientError } from '../src/index';
import { mergeSkillUpdates } from '../src/utils/skill';

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

describe('skill sharing API client', () => {
  it('toggles sharing and reads the public projection', async () => {
    const fetchMock = respond({ skill: { id: 's1' }, shareUrl: '/skill/s1' });

    await api.shareSkill('s1', true);
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skills/s1/share'
    );
    expect(lastCall(fetchMock).body).toEqual({ isShared: true });

    await api.getSharedSkill('s1');
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skill/share/s1'
    );
  });

  it('switches version routes between owned and shared skills', async () => {
    const fetchMock = respond([]);

    await api.fetchSkillVersions('s1');
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skills/s1/versions'
    );

    await api.fetchSkillVersions('shared-1', { shared: true });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skill/share/shared-1/versions'
    );

    await api.fetchSkillVersion('shared-1', 'v9', { shared: true });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skill/share/shared-1/versions/v9'
    );
  });

  it('pins a version only when one is provided', async () => {
    const fetchMock = respond({ id: 'copy' });

    await api.addSharedSkill('shared-1', 'v2');
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skill/share/shared-1/add'
    );
    expect(lastCall(fetchMock).body).toEqual({ versionId: 'v2' });

    await api.addSharedSkill('shared-1');
    expect(lastCall(fetchMock).body).toEqual({});
  });

  it('lists updates and acknowledges one source version', async () => {
    const fetchMock = respond([]);
    await api.fetchSkillUpdates();
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skills/updates'
    );

    fetchMock.mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            skillId: 's1',
            version: 4,
            updateSeenVersionId: 'v4'
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' }
          }
        )
    );
    await expect(api.ackSkillUpdate('s1', 4)).resolves.toEqual({
      skillId: 's1',
      version: 4,
      updateSeenVersionId: 'v4'
    });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skills/s1/updates/seen'
    );
    expect(lastCall(fetchMock).body).toEqual({ version: 4 });
  });

  it('sends update-from-source fields and detaches with an empty body', async () => {
    const fetchMock = respond({ id: 's1' });

    await api.updateSkillFromSource('s1', {
      versionId: 'v2',
      confirm: true,
      expectedLocalHash: 'sha256:local'
    });
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skills/s1/update-from-source'
    );
    expect(lastCall(fetchMock).body).toEqual({
      versionId: 'v2',
      confirm: true,
      expectedLocalHash: 'sha256:local'
    });

    await api.detachSkill('s1');
    expect(lastCall(fetchMock).url).toBe(
      'http://app.test/project/skills/s1/detach'
    );
    expect(lastCall(fetchMock).body).toEqual({});
  });

  it('surfaces the stable 409 code from a review-gate conflict', async () => {
    respond({ detail: { code: 'content_changed', message: 'review' } }, 409);

    const error = await api
      .updateSkill('s1', { enabled: true, reviewedContentHash: 'stale' })
      .catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ClientError);
    expect((error as ClientError).status).toBe(409);
    expect((error as ClientError).detail).toEqual({
      code: 'content_changed',
      message: 'review'
    });
  });
});

describe('mergeSkillUpdates', () => {
  const notice = (
    skillId: string,
    latestVersion: number
  ): ISkillUpdateNotice => ({
    skillId,
    title: skillId,
    sourcePromptId: `source-${skillId}`,
    pinnedVersionId: null,
    pinnedVersion: null,
    localVersion: 1,
    latestVersionId: `v${latestVersion}`,
    latestVersion,
    seenVersionId: null,
    seenVersion: null
  });

  it('keeps an acknowledged notice the server no longer lists', () => {
    const kept = { ...notice('s1', 3), seenVersion: 3 };
    expect(mergeSkillUpdates([kept], [])).toEqual([kept]);
  });

  it('takes the newer notice and sorts by version', () => {
    const merged = mergeSkillUpdates(
      [notice('s1', 2), { ...notice('s2', 5), seenVersion: 5 }],
      [notice('s1', 4)]
    );
    expect(
      merged.map((update) => [update.skillId, update.latestVersion])
    ).toEqual([
      ['s2', 5],
      ['s1', 4]
    ]);
  });

  it('ignores a stale server notice for a version already seen', () => {
    const merged = mergeSkillUpdates([notice('s1', 4)], [notice('s1', 2)]);
    expect(merged[0].latestVersion).toBe(4);
  });
});
