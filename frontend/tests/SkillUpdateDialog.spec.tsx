import type {
  ISkill,
  ISkillVersion,
  ISkillVersionSummary
} from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SkillUpdateDialog } from '@/components/SkillUpdateDialog';

const fetchSkillVersionsMock = vi.hoisted(() => vi.fn());
const fetchSkillVersionMock = vi.hoisted(() => vi.fn());
const updateSkillFromSourceMock = vi.hoisted(() => vi.fn());
const detachSkillMock = vi.hoisted(() => vi.fn());
const onUpdatedMock = vi.hoisted(() => vi.fn());
const onOpenChangeMock = vi.hoisted(() => vi.fn());

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options ? `${key} ${Object.values(options).join(' ')}` : key
  })
}));

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@chainlit-ng/react-client', () => ({
  promptErrorCode: (error: unknown) => {
    if (!error || typeof error !== 'object' || !('detail' in error)) {
      return undefined;
    }
    const detail = error.detail;
    if (!detail || typeof detail !== 'object' || !('code' in detail)) {
      return undefined;
    }
    return typeof detail.code === 'string' ? detail.code : undefined;
  },
  useSkills: () => ({
    fetchSkillVersions: fetchSkillVersionsMock,
    fetchSkillVersion: fetchSkillVersionMock,
    updateSkillFromSource: updateSkillFromSourceMock,
    detachSkill: detachSkillMock
  })
}));

global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const versions: ISkillVersionSummary[] = [
  {
    id: 'sv2',
    promptId: 'source-1',
    kind: 'skill',
    version: 2,
    title: 'linked-skill',
    description: '',
    contentHash: 'sha256:new',
    createdAt: '2026-02-01T00:00:00Z'
  },
  {
    id: 'sv1',
    promptId: 'source-1',
    kind: 'skill',
    version: 1,
    title: 'linked-skill',
    description: '',
    contentHash: 'sha256:old',
    createdAt: '2026-01-01T00:00:00Z'
  }
];

const targetVersion: ISkillVersion = {
  ...versions[0],
  content: '# Skill\nnew line'
};

const skill: ISkill = {
  id: 's1',
  title: 'linked-skill',
  content: '# Skill\nold line',
  description: '',
  userId: 'u1',
  isShared: false,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
  enabled: true,
  sourcePromptId: 'source-1',
  pinnedVersionId: 'sv1',
  pinnedContentHash: 'sha256:old',
  contentHash: 'sha256:old',
  version: 1
};

const renderDialog = (
  overrides: { skill?: ISkill; initialVersionId?: string } = {}
) =>
  render(
    <SkillUpdateDialog
      open
      skill={overrides.skill ?? skill}
      initialVersionId={overrides.initialVersionId ?? null}
      onOpenChange={onOpenChangeMock}
      onUpdated={onUpdatedMock}
    />
  );

describe('SkillUpdateDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchSkillVersionsMock.mockResolvedValue(versions);
    fetchSkillVersionMock.mockImplementation(
      async (_id: string, versionId: string) =>
        versionId === 'sv2'
          ? targetVersion
          : { ...versions[1], content: '# Skill\nold line' }
    );
    updateSkillFromSourceMock.mockResolvedValue({
      ...skill,
      enabled: false,
      content: targetVersion.content,
      version: 2,
      pinnedVersionId: 'sv2',
      pinnedContentHash: 'sha256:new',
      contentHash: 'sha256:new'
    });
    detachSkillMock.mockResolvedValue({
      ...skill,
      sourcePromptId: null,
      pinnedVersionId: null,
      pinnedContentHash: null
    });
  });

  it('loads shared versions and diffs the local body', async () => {
    renderDialog();

    expect(await screen.findByTestId('skill-update-version')).toHaveTextContent(
      'v2'
    );
    expect(fetchSkillVersionsMock).toHaveBeenCalledWith('source-1', {
      shared: true
    });
    expect(fetchSkillVersionMock).toHaveBeenCalledWith('source-1', 'sv2', {
      shared: true
    });
    const rows = await screen.findAllByTestId('skill-update-row');
    expect(rows.some((row) => row.dataset.op === 'replace')).toBe(true);
    expect(screen.getByText('new line')).toBeInTheDocument();
  });

  it('updates only after an explicit action and reports the new skill', async () => {
    renderDialog();

    const submit = await screen.findByTestId('skill-update-submit');
    await waitFor(() => expect(submit).toBeEnabled());
    expect(submit).toHaveTextContent('chat.skills.update.update 2');

    fireEvent.click(submit);

    await waitFor(() =>
      expect(updateSkillFromSourceMock).toHaveBeenCalledWith('s1', {
        versionId: 'sv2',
        confirm: false,
        expectedLocalHash: 'sha256:old'
      })
    );
    expect(onUpdatedMock).toHaveBeenCalled();
    expect(onOpenChangeMock).toHaveBeenCalledWith(false);
  });

  it('supports side-by-side rows and switching versions', async () => {
    renderDialog();

    await screen.findAllByTestId('skill-update-row');
    fireEvent.click(screen.getByTestId('skill-update-mode-split'));
    expect(screen.getByTestId('skill-update-mode-split')).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(
      document.querySelector(
        '[data-testid="skill-update-row"] td[data-side="old"]'
      )
    ).not.toBeNull();

    fireEvent.pointerDown(screen.getByTestId('skill-update-version'));
    fireEvent.click(
      await screen.findByTestId('skill-update-version-option-sv1')
    );
    await waitFor(() =>
      expect(fetchSkillVersionMock).toHaveBeenCalledWith('source-1', 'sv1', {
        shared: true
      })
    );
    expect(
      await screen.findByTestId('skill-update-already-current')
    ).toHaveTextContent('chat.skills.update.currentVersion');
    expect(screen.queryByTestId('skill-update-submit')).toBeNull();
  });

  it('explains when a selected skill version disappeared', async () => {
    fetchSkillVersionMock.mockRejectedValue({
      status: 404,
      detail: 'Skill version not found'
    });

    renderDialog();

    expect(
      await screen.findByText('chat.skills.update.versionUnavailable')
    ).toBeInTheDocument();
  });

  it('keeps the only skill version visible and explains it is current', async () => {
    const localSkill: ISkill = {
      ...skill,
      id: 'one-version',
      sourcePromptId: null,
      pinnedVersionId: null,
      pinnedContentHash: null,
      contentHash: 'sha256:current'
    };
    fetchSkillVersionsMock.mockResolvedValue([
      {
        ...versions[0],
        id: 'only-v1',
        promptId: 'one-version',
        version: 1,
        contentHash: 'sha256:current'
      }
    ]);
    fetchSkillVersionMock.mockResolvedValue({
      ...targetVersion,
      id: 'only-v1',
      contentHash: 'sha256:current',
      content: skill.content
    });

    renderDialog({ skill: localSkill });

    expect(await screen.findByTestId('skill-update-version')).toHaveTextContent(
      'v1'
    );
    expect(
      await screen.findByTestId('skill-update-already-current')
    ).toHaveTextContent('chat.skills.update.currentVersion');
    expect(screen.queryByTestId('skill-update-submit')).toBeNull();
  });

  it('labels an older target as a downgrade', async () => {
    renderDialog({ skill: { ...skill, contentHash: 'sha256:new' } });

    fireEvent.pointerDown(await screen.findByTestId('skill-update-version'));
    fireEvent.click(
      await screen.findByTestId('skill-update-version-option-sv1')
    );

    expect(await screen.findByTestId('skill-update-submit')).toHaveTextContent(
      'chat.skills.update.downgrade'
    );
  });

  it('defaults a current pinned skill to its current version', async () => {
    renderDialog({ skill: { ...skill, contentHash: 'sha256:new' } });

    expect(await screen.findByTestId('skill-update-version')).toHaveTextContent(
      'v2'
    );
    expect(
      await screen.findByTestId('skill-update-already-current')
    ).toBeInTheDocument();
    expect(screen.queryByTestId('skill-update-submit')).toBeNull();
  });

  it('shows local skill history without an update action', async () => {
    const localSkill: ISkill = {
      ...skill,
      id: 'local-skill',
      sourcePromptId: null,
      pinnedVersionId: null,
      pinnedContentHash: null,
      contentHash: 'sha256:current'
    };
    fetchSkillVersionsMock.mockResolvedValue([
      {
        ...versions[0],
        id: 'lv2',
        promptId: 'local-skill',
        contentHash: 'sha256:current'
      },
      {
        ...versions[1],
        id: 'lv1',
        promptId: 'local-skill',
        contentHash: 'sha256:old'
      }
    ]);
    fetchSkillVersionMock.mockResolvedValue({
      ...versions[1],
      id: 'lv1',
      content: '# Skill\nfirst line'
    });

    renderDialog({ skill: localSkill });

    expect(
      await screen.findByText('chat.skills.update.historyTitle')
    ).toBeInTheDocument();
    expect(screen.getByTestId('skill-update-version')).toHaveTextContent('v1');
    expect(screen.queryByTestId('skill-update-submit')).toBeNull();
  });

  it('warns about local edits and requires an explicit confirmation', async () => {
    updateSkillFromSourceMock.mockRejectedValueOnce({
      status: 409,
      detail: { code: 'local_changes', message: 'local edits' }
    });
    renderDialog({
      skill: { ...skill, contentHash: 'sha256:local' }
    });

    expect(
      await screen.findByTestId('skill-update-local-warning')
    ).toBeInTheDocument();
    const submit = await screen.findByTestId('skill-update-submit');
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    const confirm = await screen.findByTestId('skill-update-confirm');
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(updateSkillFromSourceMock).toHaveBeenLastCalledWith('s1', {
        versionId: 'sv2',
        confirm: true,
        expectedLocalHash: 'sha256:local'
      })
    );
  });

  it('refreshes the comparison when the preview went stale', async () => {
    updateSkillFromSourceMock.mockRejectedValueOnce({
      status: 409,
      detail: { code: 'preview_stale', message: 'stale' }
    });
    renderDialog();

    const submit = await screen.findByTestId('skill-update-submit');
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    expect(await screen.findByTestId('skill-update-stale')).toHaveTextContent(
      'chat.skills.update.stale'
    );
    await waitFor(() =>
      expect(fetchSkillVersionsMock).toHaveBeenCalledTimes(2)
    );
  });

  it('offers detach when the shared source disappeared', async () => {
    updateSkillFromSourceMock.mockRejectedValueOnce({
      status: 409,
      detail: { code: 'source_unavailable', message: 'gone' }
    });
    renderDialog();

    const submit = await screen.findByTestId('skill-update-submit');
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    const detach = await screen.findByTestId('skill-update-detach');
    fireEvent.click(detach);
    await waitFor(() => expect(detachSkillMock).toHaveBeenCalledWith('s1'));
    expect(onUpdatedMock).toHaveBeenCalled();
  });

  it('reports a version that does not belong to the source', async () => {
    updateSkillFromSourceMock.mockRejectedValueOnce({
      status: 409,
      detail: { code: 'version_conflict', message: 'wrong version' }
    });
    renderDialog();

    const submit = await screen.findByTestId('skill-update-submit');
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    expect(
      await screen.findByText('chat.skills.update.versionConflict')
    ).toBeInTheDocument();
  });

  it('does not offer an update when the local copy already matches', async () => {
    renderDialog({
      skill: { ...skill, contentHash: 'sha256:new' }
    });

    fireEvent.pointerDown(await screen.findByTestId('skill-update-version'));
    fireEvent.click(
      await screen.findByTestId('skill-update-version-option-sv2')
    );

    expect(
      await screen.findByTestId('skill-update-already-current')
    ).toHaveTextContent('chat.skills.update.currentVersion');
    expect(screen.queryByTestId('skill-update-submit')).toBeNull();
  });

  it('preselects the version named by an update notice', async () => {
    renderDialog({
      skill: { ...skill, contentHash: 'sha256:current' },
      initialVersionId: 'sv1'
    });

    expect(await screen.findByTestId('skill-update-version')).toHaveTextContent(
      'v1'
    );
    expect(fetchSkillVersionMock).toHaveBeenCalledWith('source-1', 'sv1', {
      shared: true
    });
  });
});
