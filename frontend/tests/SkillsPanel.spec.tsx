import { MAX_SKILL_MARKDOWN_BYTES } from '@/lib/skills';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ISkill } from '@chainlit/react-client';

import { SkillsPanel } from '@/components/chat/MessageComposer/SkillsPanel';

const state = vi.hoisted(() => ({ skills: [] as any[] }));

const fetchSkills = vi.fn();
const createSkill = vi.fn();
const updateSkill = vi.fn();
const deleteSkill = vi.fn();
const importSkill = vi.fn();
const exportSkill = vi.fn();

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

vi.mock('@chainlit/react-client', () => ({
  useConfig: () => ({ config: { features: { latex: false } } }),
  useSkills: () => ({
    skills: state.skills,
    fetchSkills,
    createSkill,
    updateSkill,
    deleteSkill,
    importSkill,
    exportSkill
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
  contentHash: 'a'.repeat(64)
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
  enabled: true
};

describe('SkillsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.skills = [];
    fetchSkills.mockResolvedValue([]);
    createSkill.mockResolvedValue(own);
    updateSkill.mockResolvedValue(own);
    deleteSkill.mockResolvedValue(undefined);
    importSkill.mockResolvedValue(imported);
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
    expect(row).toHaveTextContent(imported.contentHash!.slice(0, 12));

    expect(
      screen.getByRole('switch', { name: 'chat.skills.toggle imported-skill' })
    ).not.toBeChecked();
  });

  it('enables a skill through the API', async () => {
    state.skills = [imported];
    render(<SkillsPanel />);

    fireEvent.click(
      await screen.findByRole('switch', {
        name: 'chat.skills.toggle imported-skill'
      })
    );

    await waitFor(() =>
      expect(updateSkill).toHaveBeenCalledWith('s1', { enabled: true })
    );
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

    fireEvent.click(
      await screen.findByRole('button', { name: 'chat.skills.edit' })
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
