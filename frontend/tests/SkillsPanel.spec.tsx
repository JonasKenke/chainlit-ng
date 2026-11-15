import { MAX_SKILL_MARKDOWN_BYTES } from '@/lib/skills';
import type { ISkill, ISkillUpdateNotice } from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SkillsPanel } from '@/components/chat/MessageComposer/SkillsPanel';

interface TestConfig {
  skillsSharing?: boolean;
  features?: { latex?: boolean };
}

const state = vi.hoisted(() => {
  const config: TestConfig = { features: { latex: false } };
  return {
    skills: [] as ISkill[],
    skillUpdates: [] as ISkillUpdateNotice[],
    config,
    requireLogin: true
  };
});

const fetchSkills = vi.fn();
const createSkill = vi.fn();
const updateSkill = vi.fn();
const deleteSkill = vi.fn();
const importSkill = vi.fn();
const exportSkill = vi.fn();
const fetchSkillUpdates = vi.fn();
const fetchSkillVersions = vi.fn();
const fetchSkillVersion = vi.fn();
const detachSkill = vi.fn();
const updateSkillFromSource = vi.fn();
const shareSkill = vi.fn();

vi.mock('@/components/i18n/Translator', () => ({
  default: ({ path }: { path: string }) => path,
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options ? `${key} ${Object.values(options).join(' ')}` : key
  })
}));

vi.mock('@/components/Markdown', () => ({
  Markdown: ({ children }: { children: string }) => <pre>{children}</pre>
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() }
}));

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
  useConfig: () => ({ config: state.config }),
  useAuth: () => ({
    data: { requireLogin: state.requireLogin },
    isAuthenticated: state.requireLogin
  }),
  useSkills: () => ({
    skills: state.skills,
    skillUpdates: state.skillUpdates,
    fetchSkills,
    createSkill,
    updateSkill,
    deleteSkill,
    importSkill,
    exportSkill,
    fetchSkillUpdates,
    fetchSkillVersions,
    fetchSkillVersion,
    detachSkill,
    updateSkillFromSource,
    shareSkill
  })
}));

global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
window.HTMLElement.prototype.scrollIntoView = vi.fn();

const imported: ISkill = {
  id: 's1',
  title: 'imported-skill',
  content: '# Imported body',
  description: 'A skill that came from a file.',
  userId: 'u1',
  isShared: false,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
  enabled: false,
  importedFrom: 'review.md',
  importedAt: '2026-01-02T03:04:05Z',
  contentHash: `sha256:${'a'.repeat(64)}`
};

const own: ISkill = {
  id: 's2',
  title: 'my-skill',
  content: '# My body',
  description: 'A skill I wrote.',
  userId: 'u1',
  isShared: false,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
  enabled: true,
  contentHash: 'b'.repeat(64)
};

const pinned: ISkill = {
  id: 's3',
  title: 'linked-skill',
  content: '# Linked body',
  description: 'A skill copied from a link.',
  userId: 'u1',
  isShared: false,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
  enabled: false,
  sourcePromptId: 'source-1',
  pinnedVersionId: 'sv1',
  pinnedContentHash: `sha256:${'c'.repeat(64)}`,
  contentHash: `sha256:${'c'.repeat(64)}`
};

const notice: ISkillUpdateNotice = {
  skillId: 's3',
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

describe('SkillsPanel', () => {
  const openSkillActions = (id: string) => {
    fireEvent.pointerDown(screen.getByTestId(`skill-actions-${id}`), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    });
  };

  const openSkillFilter = () => {
    fireEvent.pointerDown(screen.getByTestId('skill-filter'), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    });
  };

  beforeEach(() => {
    vi.clearAllMocks();
    state.skills = [];
    state.skillUpdates = [];
    state.config = { features: { latex: false } };
    state.requireLogin = true;
    fetchSkills.mockResolvedValue([]);
    fetchSkillUpdates.mockResolvedValue([]);
    fetchSkillVersions.mockImplementation(async (id: string) =>
      id === 'source-1'
        ? [
            {
              id: 'sv2',
              promptId: 'source-1',
              kind: 'skill',
              version: 2,
              title: 'linked-skill',
              description: '',
              contentHash: 'd'.repeat(64),
              createdAt: '2026-02-01T00:00:00Z'
            },
            {
              id: 'sv1',
              promptId: 'source-1',
              kind: 'skill',
              version: 1,
              title: 'linked-skill',
              description: '',
              contentHash: 'c'.repeat(64),
              createdAt: '2026-01-01T00:00:00Z'
            }
          ]
        : [
            {
              id: 'v1',
              promptId: 's1',
              kind: 'skill',
              version: 1,
              title: 'imported-skill',
              description: '',
              contentHash: `sha256:${'a'.repeat(64)}`,
              createdAt: '2026-01-01T00:00:00Z'
            }
          ]
    );
    fetchSkillVersion.mockResolvedValue({
      id: 'sv2',
      promptId: 'source-1',
      kind: 'skill',
      version: 2,
      title: 'linked-skill',
      description: '',
      contentHash: 'd'.repeat(64),
      createdAt: '2026-02-01T00:00:00Z',
      content: '# Linked body v2'
    });
    createSkill.mockResolvedValue(own);
    updateSkill.mockResolvedValue(own);
    deleteSkill.mockResolvedValue(undefined);
    importSkill.mockResolvedValue(imported);
    detachSkill.mockResolvedValue(own);
    updateSkillFromSource.mockResolvedValue(pinned);
    shareSkill.mockResolvedValue({ skill: own, shareUrl: '/skill/s2' });
    exportSkill.mockResolvedValue({
      markdown: '---\nname: my-skill\n---\n',
      filename: 'my-skill.md'
    });
  });

  it('shows imported skills disabled with provenance', async () => {
    state.skills = [imported];
    render(<SkillsPanel />);

    const row = await screen.findByTestId('skill-row');
    expect(row).toHaveTextContent('imported-skill');
    expect(row).toHaveTextContent('chat.skills.importedBadge');
    expect(row).toHaveTextContent('review.md');
    expect(row).toHaveTextContent('chat.skills.reviewBeforeEnabling');
    expect(row).not.toHaveTextContent('sha256');

    expect(
      screen.getByRole('switch', { name: 'chat.skills.toggle imported-skill' })
    ).not.toBeChecked();
  });

  it('opens the review gate instead of enabling an imported skill directly', async () => {
    state.skills = [imported];
    render(<SkillsPanel />);

    fireEvent.click(
      await screen.findByRole('switch', {
        name: 'chat.skills.toggle imported-skill'
      })
    );

    expect(
      await screen.findByTestId('skill-review-dialog')
    ).toBeInTheDocument();
    expect(updateSkill).not.toHaveBeenCalled();
  });

  it('enables a reviewed imported skill with the reviewed fingerprint', async () => {
    state.skills = [imported];
    render(<SkillsPanel />);

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'chat.skills.review.open imported-skill'
      })
    );
    const enable = await screen.findByRole('button', {
      name: 'chat.skills.review.enable'
    });
    await waitFor(() => expect(enable).toBeEnabled());
    fireEvent.click(enable);

    await waitFor(() =>
      expect(updateSkill).toHaveBeenCalledWith('s1', {
        enabled: true,
        reviewedContentHash: `sha256:${'a'.repeat(64)}`
      })
    );
  });

  it('reloads and keeps the gate when the server reports a changed hash', async () => {
    state.skills = [imported];
    updateSkill.mockRejectedValueOnce({
      status: 409,
      detail: { code: 'content_changed', message: 'changed' }
    });
    render(<SkillsPanel />);

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'chat.skills.review.open imported-skill'
      })
    );
    const enable = await screen.findByRole('button', {
      name: 'chat.skills.review.enable'
    });
    await waitFor(() => expect(enable).toBeEnabled());
    fireEvent.click(enable);

    await waitFor(() => expect(fetchSkills).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('skill-review-dialog')).toBeInTheDocument();
  });

  it('filters the list into active, review and updates sections', async () => {
    state.skills = [own, imported, pinned];
    state.skillUpdates = [notice];
    render(<SkillsPanel />);

    await screen.findAllByTestId('skill-row');
    expect(screen.queryByTestId('skill-sections')).toBeNull();
    expect(
      screen.getByRole('button', {
        name: 'chat.skills.filter.current chat.skills.sections.all'
      })
    ).toBeInTheDocument();

    openSkillFilter();
    fireEvent.click(screen.getByTestId('skill-section-active'));
    expect(screen.getAllByTestId('skill-row')).toHaveLength(1);
    expect(screen.getByTestId('skill-row')).toHaveTextContent('my-skill');

    openSkillFilter();
    fireEvent.click(screen.getByTestId('skill-section-review'));
    const reviewRows = screen.getAllByTestId('skill-row');
    expect(reviewRows).toHaveLength(2);
    expect(reviewRows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('imported-skill'),
      expect.stringContaining('linked-skill')
    ]);

    openSkillFilter();
    fireEvent.click(screen.getByTestId('skill-section-updates'));
    expect(screen.getByTestId('skill-row')).toHaveTextContent('linked-skill');
    expect(screen.getByTestId('skill-update-chip-s3')).toHaveTextContent(
      'chat.skills.updateAvailable 2'
    );
  });

  it('detaches a linked copy', async () => {
    state.skills = [pinned];
    state.config = { skillsSharing: true, features: { latex: false } };
    render(<SkillsPanel />);

    await screen.findByTestId('skill-row');
    expect(screen.queryByTestId('skill-pin-s3')).toBeNull();
    expect(screen.queryByText(/sha256/)).toBeNull();

    openSkillActions('s3');
    fireEvent.click(await screen.findByTestId('skill-detach-s3'));
    await waitFor(() => expect(detachSkill).toHaveBeenCalledWith('s3'));
  });

  it('opens the update dialog from the quiet update marker', async () => {
    state.skills = [pinned];
    state.skillUpdates = [notice];
    state.config = { skillsSharing: true, features: { latex: false } };
    render(<SkillsPanel />);

    fireEvent.click(await screen.findByTestId('skill-update-chip-s3'));
    expect(
      await screen.findByTestId('skill-update-dialog')
    ).toBeInTheDocument();
  });

  it('hides sharing actions when the capability flag is off', async () => {
    state.skills = [own];
    render(<SkillsPanel />);

    await screen.findByTestId('skill-row');
    openSkillActions('s2');
    expect(screen.queryByTestId('skill-share-s2')).toBeNull();
    expect(screen.queryByTestId('skill-detach-s2')).toBeNull();
    expect(
      screen.getByRole('menuitem', { name: 'chat.skills.edit' })
    ).toBeInTheDocument();
  });

  it('hides share links but keeps editing without authenticated users', async () => {
    state.requireLogin = false;
    state.config = { skillsSharing: true, features: { latex: false } };
    state.skills = [own];
    render(<SkillsPanel />);

    await screen.findByTestId('skill-row');
    openSkillActions('s2');
    expect(screen.queryByTestId('skill-share-s2')).toBeNull();
    expect(
      screen.getByRole('menuitem', { name: 'chat.skills.edit' })
    ).toBeInTheDocument();
  });

  it('validates inline before creating and then saves', async () => {
    render(<SkillsPanel />);
    await screen.findByText('chat.skills.empty.title');

    fireEvent.click(screen.getByRole('button', { name: /chat.skills.new/ }));
    fireEvent.change(screen.getByLabelText('chat.skills.editor.name'), {
      target: { value: 'Bad Name' }
    });
    fireEvent.change(screen.getByLabelText('chat.skills.editor.description'), {
      target: { value: 'A description' }
    });
    fireEvent.click(
      screen.getByRole('button', { name: /chat.skills.editor.save/ })
    );

    expect(
      await screen.findByText('chat.skills.validation.namePattern')
    ).toBeInTheDocument();
    expect(createSkill).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('chat.skills.editor.name'), {
      target: { value: 'good-name' }
    });
    fireEvent.change(screen.getByLabelText('chat.skills.editor.body'), {
      target: { value: '# Body' }
    });
    fireEvent.click(
      screen.getByRole('button', { name: /chat.skills.editor.save/ })
    );

    await waitFor(() =>
      expect(createSkill).toHaveBeenCalledWith({
        name: 'good-name',
        description: 'A description',
        content: '# Body',
        enabled: true
      })
    );
  });

  it('shows the backend error for an invalid import inline', async () => {
    importSkill.mockRejectedValue({
      status: 400,
      detail: 'imported skill: missing required frontmatter field name'
    });
    render(<SkillsPanel />);
    await screen.findByText('chat.skills.empty.title');

    const input = document.querySelector(
      '#skill-import-input'
    ) as HTMLInputElement;
    const file = {
      name: 'broken.md',
      text: async () => '---\ndescription: no name\n---\n'
    } as unknown as File;
    fireEvent.change(input, { target: { files: [file] } });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'missing required frontmatter field name'
    );
    expect(importSkill).toHaveBeenCalledWith(
      '---\ndescription: no name\n---\n',
      'broken.md'
    );
  });

  it('rejects an oversized import before reading it', async () => {
    render(<SkillsPanel />);
    await screen.findByText('chat.skills.empty.title');

    const input = document.querySelector(
      '#skill-import-input'
    ) as HTMLInputElement;
    const text = vi.fn();
    const file = {
      name: 'huge.md',
      size: MAX_SKILL_MARKDOWN_BYTES + 1,
      text
    } as unknown as File;
    fireEvent.change(input, { target: { files: [file] } });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'chat.skills.validation.fileTooLarge'
    );
    expect(text).not.toHaveBeenCalled();
    expect(importSkill).not.toHaveBeenCalled();
  });

  it('edits an existing skill', async () => {
    state.skills = [own];
    render(<SkillsPanel />);

    await screen.findByTestId('skill-row');
    openSkillActions('s2');
    fireEvent.click(
      await screen.findByRole('menuitem', { name: 'chat.skills.edit' })
    );
    fireEvent.change(screen.getByLabelText('chat.skills.editor.description'), {
      target: { value: 'Updated description' }
    });
    fireEvent.click(
      screen.getByRole('button', { name: /chat.skills.editor.save/ })
    );

    await waitFor(() =>
      expect(updateSkill).toHaveBeenCalledWith('s2', {
        name: 'my-skill',
        description: 'Updated description',
        content: '# My body'
      })
    );
  });
});
