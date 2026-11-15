import type {
  IPrompt,
  IPromptUpdate,
  ISkillUpdateNotice
} from '@chainlit-ng/react-client';
import { useConfig } from '@chainlit-ng/react-client';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PromptGalleryButton } from '@/components/chat/MessageComposer/PromptGalleryButton';

const fetchPromptsMock = vi.fn().mockResolvedValue(undefined);
const fetchSkillsMock = vi.fn().mockResolvedValue([]);
const removePromptMock = vi.fn().mockResolvedValue(undefined);
const sharePromptMock = vi.fn().mockResolvedValue('/prompt/some-id');
const fetchPromptUpdatesMock = vi.fn().mockResolvedValue([]);
const ackPromptUpdateMock = vi.fn().mockResolvedValue(2);
const fetchSkillUpdatesMock = vi.fn().mockResolvedValue([]);
const ackSkillUpdateMock = vi.fn().mockResolvedValue(2);
const clientState = vi.hoisted(() => ({
  prompts: [] as IPrompt[],
  promptUpdates: [] as IPromptUpdate[],
  skillUpdates: [] as ISkillUpdateNotice[]
}));
const fetchPromptVersionsMock = vi.fn().mockResolvedValue([
  {
    id: 'v2',
    promptId: 'p1',
    kind: 'prompt',
    version: 2,
    title: 'Summarise',
    description: '',
    contentHash: 'sha256:new',
    createdAt: '2024-02-01T00:00:00Z'
  }
]);
const fetchPromptDiffMock = vi.fn().mockResolvedValue({
  promptId: 'p1',
  from: 'pinned',
  to: 'v2',
  fromVersionId: 'v1',
  toVersionId: 'v2',
  fromVersion: 1,
  toVersion: 2,
  fromContentHash: 'sha256:old',
  toContentHash: 'sha256:new',
  rows: [
    {
      op: 'replace',
      oldLine: 1,
      newLine: 1,
      oldText: 'old line',
      newText: 'new line'
    }
  ],
  truncated: false,
  maxRows: 2000,
  maxBytes: 262144
});
const toastMock = vi.hoisted(() => vi.fn());
const editPromptMock = vi.fn().mockResolvedValue({
  id: 'p1',
  title: 'Updated',
  content: 'New content',
  userId: 'u1',
  isShared: false,
  createdAt: '',
  updatedAt: ''
});

const savePromptMock = vi.fn().mockResolvedValue({
  id: 'p2',
  title: 'New',
  content: 'New content',
  userId: 'u1',
  isShared: false,
  createdAt: '',
  updatedAt: ''
});

vi.mock('@/components/i18n/Translator', () => ({
  default: ({ path }: { path: string }) => path,
  useTranslation: () => ({
    t: (key: string) => key
  })
}));

vi.mock('sonner', () => ({ toast: toastMock }));

vi.mock('@chainlit-ng/react-client', () => ({
  useConfig: vi.fn(),
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
  usePromptGallery: () => ({
    fetchPrompts: fetchPromptsMock,
    editPrompt: editPromptMock,
    removePrompt: removePromptMock,
    sharePrompt: sharePromptMock,
    savePrompt: savePromptMock,
    fetchPromptUpdates: fetchPromptUpdatesMock,
    ackPromptUpdate: ackPromptUpdateMock,
    fetchPromptVersions: fetchPromptVersionsMock,
    fetchPromptDiff: fetchPromptDiffMock
  }),
  useSkills: () => ({
    skills: [],
    skillUpdates: clientState.skillUpdates,
    fetchSkills: fetchSkillsMock,
    fetchSkillUpdates: fetchSkillUpdatesMock,
    ackSkillUpdate: ackSkillUpdateMock,
    createSkill: vi.fn(),
    updateSkill: vi.fn(),
    deleteSkill: vi.fn(),
    importSkill: vi.fn(),
    exportSkill: vi.fn(),
    fetchSkillVersions: vi.fn().mockResolvedValue([]),
    fetchSkillVersion: vi.fn(),
    detachSkill: vi.fn(),
    updateSkillFromSource: vi.fn(),
    shareSkill: vi.fn()
  }),
  useChainlitStore: (selector: (state: typeof clientState) => unknown) =>
    selector(clientState)
}));

// required for Radix UI
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
window.HTMLElement.prototype.scrollIntoView = vi.fn();

const samplePrompts: IPrompt[] = [
  {
    id: 'p1',
    title: 'Summarise',
    content: 'Summarise this: {text}',
    userId: 'u1',
    isShared: false,
    createdAt: '2024-01-01T00:00:00Z',
    updatedAt: '2024-01-01T00:00:00Z'
  }
];

describe('PromptGalleryButton', () => {
  const mockOnSelect = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const renderComponent = (
    prompts = samplePrompts,
    props = {},
    updates: IPromptUpdate[] = [],
    skillNotices: ISkillUpdateNotice[] = []
  ) => {
    clientState.prompts = prompts;
    clientState.promptUpdates = updates;
    clientState.skillUpdates = skillNotices;
    return render(<PromptGalleryButton onSelect={mockOnSelect} {...props} />);
  };

  it('returns null when promptGallery is disabled', () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: false } });
    const { container } = renderComponent();
    expect(container.firstChild).toBeNull();
    expect(fetchPromptsMock).not.toHaveBeenCalled();
    expect(fetchSkillsMock).not.toHaveBeenCalled();
  });

  it('renders and opens skills when only skills is enabled', async () => {
    (useConfig as any).mockReturnValue({
      config: { promptGallery: false, features: { skills: true } }
    });
    renderComponent();
    fireEvent.click(getGalleryBtn());

    expect(await screen.findByText('chat.skills.new')).toBeInTheDocument();
    expect(fetchPromptsMock).not.toHaveBeenCalled();
  });

  it('shows both tabs when prompts and skills are enabled', async () => {
    (useConfig as any).mockReturnValue({
      config: { promptGallery: true, features: { skills: true } }
    });
    renderComponent();
    fireEvent.click(getGalleryBtn());

    expect(await screen.findByText('chat.skills.tab')).toBeInTheDocument();
    expect(screen.getByText('chat.promptGallery.tab')).toBeInTheDocument();
  });

  const getGalleryBtn = () => document.getElementById('prompt-gallery-open')!;

  it('renders button when promptGallery is enabled', () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent();
    expect(getGalleryBtn()).toBeInTheDocument();
  });

  it('opens dialog on button click', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent();
    fireEvent.click(getGalleryBtn());
    await waitFor(() => {
      expect(screen.getByText('chat.promptGallery.title')).toBeInTheDocument();
    });
  });

  it('shows empty state when gallery has no prompts', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent([]);
    fireEvent.click(getGalleryBtn());
    await waitFor(() => {
      expect(
        screen.getByText('chat.promptGallery.empty.title')
      ).toBeInTheDocument();
    });
  });

  it('shows prompt list on open', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent(samplePrompts);
    fireEvent.click(getGalleryBtn());
    await waitFor(() => {
      expect(screen.getByText('Summarise')).toBeInTheDocument();
    });
  });

  it('calls fetchPrompts only once on first open', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent();
    fireEvent.click(getGalleryBtn());
    await waitFor(() => expect(fetchPromptsMock).toHaveBeenCalledTimes(1));
    // close via Escape
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(getGalleryBtn());
    await act(() => Promise.resolve());
    expect(fetchPromptsMock).toHaveBeenCalledTimes(1);
  });

  it('calls onSelect when a prompt is clicked', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent(samplePrompts);
    fireEvent.click(getGalleryBtn());
    await waitFor(() => screen.getByText('Summarise'));
    fireEvent.click(screen.getByText('Summarise'));
    expect(mockOnSelect).toHaveBeenCalledWith('Summarise this: {text}');
  });

  it('respects disabled prop', () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent(samplePrompts, { disabled: true });
    expect(getGalleryBtn()).toBeDisabled();
  });

  it('reopens gallery when edit dialog is closed/cancelled', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent(samplePrompts);
    // Open gallery
    fireEvent.click(getGalleryBtn());
    await waitFor(() => screen.getByText('Summarise'));
    // Click edit on the prompt
    const editBtn = screen.getByRole('button', {
      name: 'chat.promptGallery.edit'
    });
    fireEvent.click(editBtn);
    // Gallery closed, edit dialog open
    await waitFor(() => screen.getByText('chat.promptGallery.edit'));
    // Cancel the edit dialog
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptGallery.dialog.cancel' })
    );
    // Gallery should reopen
    await waitFor(() =>
      expect(screen.getByText('chat.promptGallery.title')).toBeInTheDocument()
    );
  });

  it('reopens gallery after a successful edit save', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent(samplePrompts);
    fireEvent.click(getGalleryBtn());
    await waitFor(() => screen.getByText('Summarise'));
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptGallery.edit' })
    );
    await waitFor(() => screen.getByText('chat.promptGallery.edit'));
    // Save
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptGallery.dialog.save' })
    );
    await waitFor(() =>
      expect(screen.getByText('chat.promptGallery.title')).toBeInTheDocument()
    );
  });

  it('gallery stays open when New Prompt is clicked (save dialog stacks on top)', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent([]);
    fireEvent.click(getGalleryBtn());
    await waitFor(() => screen.getByText('chat.promptGallery.new'));
    fireEvent.click(screen.getByText('chat.promptGallery.new'));
    // Gallery title is still visible (gallery stays open)
    expect(screen.getByText('chat.promptGallery.title')).toBeInTheDocument();
    // Save dialog also opens on top
    await waitFor(() => {
      expect(screen.getByText('chat.promptGallery.save')).toBeInTheDocument();
    });
  });

  it('shows New Prompt button in open dialog', async () => {
    (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
    renderComponent([]);
    fireEvent.click(getGalleryBtn());
    await waitFor(() => {
      expect(screen.getByText('chat.promptGallery.new')).toBeInTheDocument();
    });
  });

  describe('prompt updates', () => {
    const versioningConfig = {
      config: { promptGallery: true, promptVersioning: true }
    };
    const updateNotice: IPromptUpdate = {
      promptId: 'p1',
      title: 'Summarise',
      sourcePromptId: 'source-1',
      pinnedVersionId: 'v1',
      pinnedVersion: 1,
      localVersion: 1,
      latestVersionId: 'v2',
      latestVersion: 2,
      notifiedVersion: 0
    };
    const secondNotice: IPromptUpdate = {
      ...updateNotice,
      promptId: 'p2',
      title: 'Translate',
      latestVersionId: 'v5',
      latestVersion: 5
    };

    it('shows a quiet dot when an update is available', () => {
      (useConfig as any).mockReturnValue(versioningConfig);
      renderComponent(samplePrompts, {}, [updateNotice]);
      expect(
        screen.getByTestId('prompt-gallery-update-dot')
      ).toBeInTheDocument();
    });

    it('shows no dot when there are no updates', () => {
      (useConfig as any).mockReturnValue(versioningConfig);
      renderComponent(samplePrompts, {}, []);
      expect(screen.queryByTestId('prompt-gallery-update-dot')).toBeNull();
    });

    it('fetches updates once on mount and again when the dialog opens', async () => {
      (useConfig as any).mockReturnValue(versioningConfig);
      renderComponent(samplePrompts, {}, []);
      await waitFor(() =>
        expect(fetchPromptUpdatesMock).toHaveBeenCalledTimes(1)
      );
      fireEvent.click(getGalleryBtn());
      await waitFor(() =>
        expect(fetchPromptUpdatesMock).toHaveBeenCalledTimes(2)
      );
    });

    it('shows one grouped toast per new version and acknowledges each', async () => {
      (useConfig as any).mockReturnValue(versioningConfig);
      renderComponent(samplePrompts, {}, [updateNotice, secondNotice]);

      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      const [message, options] = toastMock.mock.calls[0];
      expect(message).toBe('chat.promptGallery.updatesToast');
      expect(options.action.label).toBe(
        'chat.promptGallery.updatesToastAction'
      );
      expect(options.id).toBe('prompt-gallery-updates');

      await waitFor(() => expect(ackPromptUpdateMock).toHaveBeenCalledTimes(2));
      expect(ackPromptUpdateMock).toHaveBeenCalledWith('p1', 2);
      expect(ackPromptUpdateMock).toHaveBeenCalledWith('p2', 5);

      // Re-rendering must not emit a second toast for the same versions.
      expect(toastMock).toHaveBeenCalledTimes(1);
    });

    it('does not toast when versioning is unavailable', async () => {
      (useConfig as any).mockReturnValue({ config: { promptGallery: true } });
      renderComponent(samplePrompts, {}, [updateNotice]);
      await act(() => Promise.resolve());
      expect(fetchPromptUpdatesMock).not.toHaveBeenCalled();
      expect(toastMock).not.toHaveBeenCalled();
    });

    it('shows a per-row chip and opens the version diff', async () => {
      (useConfig as any).mockReturnValue(versioningConfig);
      renderComponent(samplePrompts, {}, [updateNotice]);
      fireEvent.click(getGalleryBtn());

      const chip = await screen.findByTestId('prompt-update-chip-p1');
      expect(chip).toHaveTextContent('chat.promptGallery.updateAvailable');
      fireEvent.click(chip);

      expect(
        await screen.findByTestId('prompt-diff-dialog')
      ).toBeInTheDocument();
      expect(await screen.findByText('new line')).toBeInTheDocument();
    });
  });

  describe('library shell', () => {
    const libraryConfig = {
      config: {
        promptGallery: true,
        promptVersioning: true,
        features: { skills: true },
        skillsSharing: true
      }
    };
    const skillNotice: ISkillUpdateNotice = {
      skillId: 's1',
      title: 'linked-skill',
      sourcePromptId: 'source-1',
      pinnedVersionId: 'sv1',
      pinnedVersion: 1,
      localVersion: 1,
      latestVersionId: 'sv2',
      latestVersion: 2,
      seenVersionId: 'sv1',
      seenVersion: 1
    };

    it('presents one Library dialog with Prompts and Skills tabs', async () => {
      (useConfig as any).mockReturnValue(libraryConfig);
      renderComponent();
      fireEvent.click(getGalleryBtn());

      expect(
        await screen.findByText('chat.promptGallery.title')
      ).toBeInTheDocument();
      expect(screen.getByText('chat.promptGallery.tab')).toBeInTheDocument();
      expect(screen.getByText('chat.skills.tab')).toBeInTheDocument();
      expect(document.querySelectorAll('#prompt-gallery-open')).toHaveLength(1);
      expect(screen.queryByTestId('skill-gallery-open')).toBeNull();
    });

    it('keeps the single quiet dot when only a skill update is available', async () => {
      (useConfig as any).mockReturnValue(libraryConfig);
      renderComponent(samplePrompts, {}, [], [skillNotice]);

      expect(
        screen.getByTestId('prompt-gallery-update-dot')
      ).toBeInTheDocument();
    });

    it('fetches skill updates on mount and again when the library opens', async () => {
      (useConfig as any).mockReturnValue(libraryConfig);
      renderComponent(samplePrompts, {}, [], []);

      await waitFor(() =>
        expect(fetchSkillUpdatesMock).toHaveBeenCalledTimes(1)
      );
      fireEvent.click(getGalleryBtn());
      await waitFor(() =>
        expect(fetchSkillUpdatesMock).toHaveBeenCalledTimes(2)
      );
    });

    it('notifies once for a skill version and acknowledges it', async () => {
      (useConfig as any).mockReturnValue(libraryConfig);
      renderComponent(samplePrompts, {}, [], [skillNotice]);

      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      const [message, options] = toastMock.mock.calls[0];
      expect(message).toBe('chat.skills.updatesToast');
      expect(options.id).toBe('skill-updates');
      expect(options.action.label).toBe('chat.skills.updatesToastAction');
      await waitFor(() =>
        expect(ackSkillUpdateMock).toHaveBeenCalledWith('s1', 2)
      );
      expect(toastMock).toHaveBeenCalledTimes(1);
    });

    it('does not repeat the skill toast for an already seen version', async () => {
      (useConfig as any).mockReturnValue(libraryConfig);
      renderComponent(
        samplePrompts,
        {},
        [],
        [{ ...skillNotice, seenVersion: 2, seenVersionId: 'sv2' }]
      );

      await act(() => Promise.resolve());
      expect(toastMock).not.toHaveBeenCalled();
      expect(ackSkillUpdateMock).not.toHaveBeenCalled();
    });

    it('falls back to prompts only when skillsLibrary is false', async () => {
      (useConfig as any).mockReturnValue({
        config: {
          promptGallery: true,
          features: { skills: true },
          skillsLibrary: false
        }
      });
      renderComponent();
      fireEvent.click(getGalleryBtn());
      expect(
        await screen.findByText('chat.promptGallery.title')
      ).toBeInTheDocument();
      expect(screen.queryByText('chat.skills.tab')).toBeNull();
      expect(fetchSkillUpdatesMock).not.toHaveBeenCalled();
    });
  });
});
