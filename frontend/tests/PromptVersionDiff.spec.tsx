import type { IPrompt } from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { PromptVersionDiff } from '@/components/PromptVersionDiff';

const mocks = vi.hoisted(() => ({
  fetchPromptVersions: vi.fn(),
  fetchPromptVersion: vi.fn(),
  syncPrompt: vi.fn(),
  detachPrompt: vi.fn(),
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn()
  })
}));

vi.mock('sonner', () => ({ toast: mocks.toast }));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({ t: (key: string) => key })
}));

vi.mock('@chainlit-ng/react-client', () => ({
  usePromptGallery: () => ({
    fetchPromptVersions: mocks.fetchPromptVersions,
    fetchPromptVersion: mocks.fetchPromptVersion,
    syncPrompt: mocks.syncPrompt,
    detachPrompt: mocks.detachPrompt
  }),
  promptErrorCode: (error: unknown) => {
    if (!error || typeof error !== 'object' || !('detail' in error)) {
      return undefined;
    }
    const detail = error.detail;
    if (!detail || typeof detail !== 'object' || !('code' in detail)) {
      return undefined;
    }
    return typeof detail.code === 'string' ? detail.code : undefined;
  }
}));

global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
window.HTMLElement.prototype.scrollIntoView = vi.fn();

const prompt: IPrompt = {
  id: 'p1',
  title: 'My copy',
  content: 'hello\nold line',
  userId: 'u1',
  isShared: false,
  createdAt: '2024-01-01T00:00:00Z',
  updatedAt: '2024-01-02T00:00:00Z',
  version: 2,
  contentHash: 'sha256:local',
  sourcePromptId: 'source-1',
  pinnedVersionId: 'v1',
  pinnedContentHash: 'sha256:pinned',
  pinnedAt: '2024-01-01T00:00:00Z'
};

const versions = [
  {
    id: 'v3',
    promptId: 'p1',
    kind: 'prompt' as const,
    version: 3,
    title: 'My copy',
    description: '',
    contentHash: 'sha256:source-new-head',
    createdAt: '2024-03-01T00:00:00Z',
    createdBy: 'owner@example.com'
  },
  {
    id: 'v2',
    promptId: 'p1',
    kind: 'prompt' as const,
    version: 2,
    title: 'My copy',
    description: '',
    contentHash: 'sha256:source-v2',
    createdAt: '2024-02-01T00:00:00Z'
  }
];

const versionContent = (id: string) =>
  id === 'v3' ? 'hello\nnew line' : id === 'v2' ? 'hello\nv2 line' : 'hello';

const renderDialog = (
  overrides: Partial<ComponentProps<typeof PromptVersionDiff>> = {}
) => {
  const onOpenChange = vi.fn();
  const onUpdated = vi.fn();
  const view = render(
    <PromptVersionDiff
      prompt={prompt}
      open
      onOpenChange={onOpenChange}
      onUpdated={onUpdated}
      {...overrides}
    />
  );
  return { ...view, onOpenChange, onUpdated };
};

describe('PromptVersionDiff', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.fetchPromptVersions.mockResolvedValue(versions);
    mocks.fetchPromptVersion.mockImplementation(
      async (_id: string, versionId: string) => ({
        id: versionId,
        content: versionContent(versionId)
      })
    );
    mocks.syncPrompt.mockResolvedValue({
      ...prompt,
      content: 'hello\nnew line'
    });
    mocks.detachPrompt.mockResolvedValue({ ...prompt, sourcePromptId: null });
  });

  it('renders the diff for the selected source version', async () => {
    renderDialog();

    expect(await screen.findByText('new line')).toBeInTheDocument();
    expect(screen.getByText('old line')).toBeInTheDocument();
    expect(mocks.fetchPromptVersion).toHaveBeenCalledWith('source-1', 'v3', {
      shared: true
    });
    expect(mocks.fetchPromptVersions).toHaveBeenCalledWith('source-1', {
      shared: true
    });
  });

  it('toggles between unified and side-by-side rendering', async () => {
    const { container } = renderDialog();
    await screen.findByText('new line');

    const unified = screen.getByTestId('prompt-diff-mode-unified');
    const split = screen.getByTestId('prompt-diff-mode-split');
    expect(unified).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(split);
    expect(split).toHaveAttribute('aria-pressed', 'true');
    const oldCells = [
      ...container.ownerDocument.querySelectorAll('[data-side="old"]')
    ].map((cell) => cell.textContent);
    expect(oldCells).toContain('old line');

    fireEvent.click(unified);
    expect(unified).toHaveAttribute('aria-pressed', 'true');
  });

  it('refetches the diff when another target version is selected', async () => {
    renderDialog();
    await screen.findByText('new line');

    fireEvent.pointerDown(screen.getByTestId('prompt-diff-version'));
    fireEvent.click(await screen.findByTestId('prompt-diff-version-option-v2'));

    await waitFor(() =>
      expect(mocks.fetchPromptVersion).toHaveBeenLastCalledWith(
        'source-1',
        'v2',
        { shared: true }
      )
    );
  });

  it('asks for confirmation before replacing local edits', async () => {
    mocks.syncPrompt
      .mockRejectedValueOnce({
        status: 409,
        detail: { code: 'local_changes', message: 'confirm' }
      })
      .mockResolvedValueOnce({ ...prompt, content: 'hello\nnew line' });

    const { onOpenChange, onUpdated } = renderDialog();
    await screen.findByText('new line');

    fireEvent.click(screen.getByTestId('prompt-diff-update'));
    expect(
      await screen.findByTestId('prompt-diff-local-warning')
    ).toBeInTheDocument();
    expect(mocks.syncPrompt.mock.calls[0][1]).toEqual({
      versionId: 'v3',
      confirm: false,
      expectedLocalHash: 'sha256:local'
    });

    fireEvent.click(screen.getByTestId('prompt-diff-confirm'));

    await waitFor(() => expect(mocks.syncPrompt).toHaveBeenCalledTimes(2));
    expect(mocks.syncPrompt.mock.calls[1][1]).toEqual({
      versionId: 'v3',
      confirm: true,
      expectedLocalHash: 'sha256:local'
    });
    await waitFor(() => expect(onUpdated).toHaveBeenCalledTimes(1));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it('refetches the diff when the preview is stale', async () => {
    mocks.syncPrompt.mockRejectedValue({
      status: 409,
      detail: { code: 'preview_stale', message: 'stale' }
    });

    renderDialog();
    await screen.findByText('new line');
    expect(mocks.fetchPromptVersion).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('prompt-diff-update'));

    expect(await screen.findByTestId('prompt-diff-stale')).toBeInTheDocument();
    await waitFor(() =>
      expect(mocks.fetchPromptVersion).toHaveBeenCalledTimes(2)
    );
    expect(mocks.fetchPromptVersions).toHaveBeenCalledTimes(2);
  });

  it('offers detach when the shared source is unavailable', async () => {
    mocks.fetchPromptVersion.mockRejectedValue({
      status: 409,
      detail: { code: 'pinned_unavailable', message: 'gone' }
    });

    const { onOpenChange, onUpdated } = renderDialog();

    fireEvent.click(await screen.findByTestId('prompt-diff-detach'));

    await waitFor(() => expect(mocks.detachPrompt).toHaveBeenCalledWith('p1'));
    await waitFor(() => expect(onUpdated).toHaveBeenCalledTimes(1));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it('reports an already-current copy without offering an update', async () => {
    mocks.syncPrompt.mockRejectedValue({
      status: 409,
      detail: { code: 'already_current', message: 'current' }
    });

    renderDialog();
    await screen.findByText('new line');

    fireEvent.click(screen.getByTestId('prompt-diff-update'));

    expect(
      await screen.findByTestId('prompt-diff-already-current')
    ).toBeInTheDocument();
    expect(screen.getByTestId('prompt-diff-update')).toBeDisabled();
  });

  it('opens local version history without an update action', async () => {
    const localPrompt: IPrompt = {
      ...prompt,
      id: 'local-1',
      sourcePromptId: null,
      pinnedVersionId: null,
      pinnedContentHash: null,
      pinnedAt: null,
      contentHash: 'sha256:source-new-head'
    };
    const localVersions = [
      { ...versions[0], id: 'lv3', promptId: 'local-1', version: 3 },
      { ...versions[1], id: 'lv2', promptId: 'local-1', version: 2 },
      { ...versions[1], id: 'lv1', promptId: 'local-1', version: 1 }
    ];
    mocks.fetchPromptVersions.mockResolvedValue(localVersions);
    mocks.fetchPromptVersion.mockResolvedValue({
      id: 'lv2',
      content: 'hello\nolder line'
    });

    renderDialog({ prompt: localPrompt });

    expect(
      await screen.findByText('chat.promptGallery.diff.historyTitle')
    ).toBeInTheDocument();
    expect(mocks.fetchPromptVersions).toHaveBeenCalledWith('local-1', {
      shared: false
    });
    await waitFor(() =>
      expect(mocks.fetchPromptVersion).toHaveBeenCalledWith('local-1', 'lv2', {
        shared: false
      })
    );
    expect(screen.getByTestId('prompt-diff-version')).toHaveTextContent('v2');
    expect(screen.queryByTestId('prompt-diff-update')).toBeNull();
  });

  it('keeps a single prompt version visible and explains it is current', async () => {
    const localPrompt: IPrompt = {
      ...prompt,
      id: 'only-prompt',
      sourcePromptId: null,
      pinnedVersionId: null,
      pinnedContentHash: null,
      pinnedAt: null,
      contentHash: 'sha256:current'
    };
    mocks.fetchPromptVersions.mockResolvedValue([
      {
        ...versions[0],
        id: 'only-v1',
        promptId: 'only-prompt',
        version: 1,
        contentHash: 'sha256:current'
      }
    ]);
    mocks.fetchPromptVersion.mockResolvedValue({
      id: 'only-v1',
      content: prompt.content
    });

    renderDialog({ prompt: localPrompt });

    expect(await screen.findByTestId('prompt-diff-version')).toHaveTextContent(
      'v1'
    );
    expect(
      await screen.findByText('chat.promptGallery.diff.currentVersion')
    ).toBeInTheDocument();
    expect(screen.queryByTestId('prompt-diff-update')).toBeNull();
  });

  it('renders untrusted diff text as text, never as HTML', async () => {
    const payload = '<script>alert("x")</script>';
    mocks.fetchPromptVersion.mockResolvedValue({
      id: 'v3',
      content: payload
    });

    renderDialog();

    const cell = await screen.findByText(payload);
    expect(cell.tagName).toBe('TD');
    expect(
      document.querySelector('[data-testid="prompt-diff-row"] script')
    ).toBeNull();
  });
});
