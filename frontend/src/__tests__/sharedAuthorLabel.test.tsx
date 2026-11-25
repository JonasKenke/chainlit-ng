import { ChainlitContext } from '@chainlit-ng/react-client';
import type {
  ChainlitAPI,
  ISharedPrompt,
  ISharedSkill
} from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import PromptSharePage from '@/pages/PromptShare';
import SkillSharePage from '@/pages/SkillShare';

import { SkillShareDialog } from '@/components/SkillShareDialog';
import { PromptGalleryButton } from '@/components/chat/MessageComposer/PromptGalleryButton';

const DISCLOSURE_KEY = 'chat.share.authorLabelDisclosure';
const DISCLOSURE_DEFAULT =
  'Anyone with the link can see the author label (your name or email).';

const mocks = vi.hoisted(() => ({
  strings: {} as Record<string, string>,
  store: {
    prompts: [] as unknown[],
    promptUpdates: [] as unknown[],
    skillUpdates: [] as unknown[]
  },
  getSharedSkill: vi.fn(),
  addSharedSkill: vi.fn(),
  fetchSkillVersions: vi.fn(),
  fetchSkillVersion: vi.fn(),
  shareSkill: vi.fn(),
  getSharedPrompt: vi.fn(),
  fetchPromptVersions: vi.fn(),
  fetchPromptVersion: vi.fn(),
  addSharedPrompt: vi.fn(),
  sharePrompt: vi.fn(),
  fetchPrompts: vi.fn(),
  copyToClipboard: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn()
}));

vi.mock('@/components/i18n/Translator', () => ({
  default: ({ path }: { path: string }) => path,
  useTranslation: () => ({
    ready: true,
    t: (key: string, options?: Record<string, unknown>) => {
      const template =
        mocks.strings[key] ??
        (typeof options?.defaultValue === 'string'
          ? options.defaultValue
          : key);
      return template.replace(/\{\{(\w+)\}\}/g, (_match, name: string) =>
        String(options?.[name] ?? `{{${name}}}`)
      );
    }
  })
}));

vi.mock('@/lib/router', () => ({ default: () => '/' }));
vi.mock('@/lib/clipboard', () => ({
  absoluteShareUrl: (path: string) => `https://app.example${path}`,
  copyToClipboard: mocks.copyToClipboard
}));
vi.mock('sonner', () => ({
  toast: Object.assign(mocks.toastSuccess, {
    success: mocks.toastSuccess,
    error: mocks.toastError
  })
}));
vi.mock('pages/Page', () => ({
  default: ({ children }: { children: ReactNode }) => <div>{children}</div>
}));
vi.mock('@/components/Markdown', () => ({
  Markdown: ({ children }: { children: ReactNode }) => <div>{children}</div>
}));
vi.mock('@/components/PromptSaveDialog', () => ({
  PromptSaveDialog: () => null
}));
vi.mock('@/components/PromptVersionDiff', () => ({
  PromptVersionDiff: () => null
}));
vi.mock('@/components/chat/MessageComposer/SkillsPanel', () => ({
  SkillsPanel: () => null
}));

vi.mock('@chainlit-ng/react-client', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    useAuth: () => ({
      data: { requireLogin: true },
      isAuthenticated: false
    }),
    useConfig: () => ({
      config: {
        promptGallery: true,
        promptVersioning: false,
        skillsSharing: false,
        features: { skills: false }
      }
    }),
    useChainlitStore: (selector: (state: typeof mocks.store) => unknown) =>
      selector(mocks.store),
    useSkills: () => ({
      getSharedSkill: mocks.getSharedSkill,
      addSharedSkill: mocks.addSharedSkill,
      fetchSkillVersions: mocks.fetchSkillVersions,
      fetchSkillVersion: mocks.fetchSkillVersion,
      shareSkill: mocks.shareSkill,
      skillUpdates: [],
      fetchSkillUpdates: vi.fn(),
      ackSkillUpdate: vi.fn()
    }),
    usePromptGallery: () => ({
      getSharedPrompt: mocks.getSharedPrompt,
      addSharedPrompt: mocks.addSharedPrompt,
      sharePrompt: mocks.sharePrompt,
      fetchPrompts: mocks.fetchPrompts,
      fetchPromptUpdates: vi.fn(),
      ackPromptUpdate: vi.fn(),
      editPrompt: vi.fn(),
      removePrompt: vi.fn(),
      savePrompt: vi.fn()
    }),
    promptErrorCode: () => undefined
  };
});

const translations = JSON.parse(
  readFileSync(
    resolve(process.cwd(), '../backend/chainlit/translations/en-US.json'),
    'utf8'
  )
) as {
  chat: {
    promptGallery: { copied: string };
    share: { authorLabelDisclosure: string };
    [key: string]: unknown;
  };
};

function flattenStrings(
  value: unknown,
  prefix = 'chat',
  output: Record<string, string> = {}
): Record<string, string> {
  if (typeof value === 'string') {
    output[prefix] = value;
  } else if (value && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      flattenStrings(child, `${prefix}.${key}`, output);
    }
  }
  return output;
}

const skill: ISharedSkill = {
  id: 'skill-1',
  title: 'Shared skill',
  description: 'Skill description',
  content: 'Skill body',
  version: 1,
  currentVersionId: 'version-1',
  contentHash: 'sha256:skill',
  updatedAt: '2024-01-01T00:00:00Z',
  authorLabel: '  Ada Lovelace  '
};

const prompt: ISharedPrompt = {
  id: 'prompt-1',
  title: 'Shared prompt',
  content: 'Prompt body',
  isShared: true,
  createdAt: '2024-01-01T00:00:00Z',
  updatedAt: '2024-01-01T00:00:00Z',
  version: 1,
  currentVersionId: 'version-1',
  currentContentHash: 'sha256:prompt',
  authorLabel: '  Ada Lovelace  '
};

const apiClient = {
  getSharedPrompt: mocks.getSharedPrompt,
  fetchPromptVersions: mocks.fetchPromptVersions,
  fetchPromptVersion: mocks.fetchPromptVersion
} as unknown as ChainlitAPI;

function renderRoute(ui: ReactNode, path: string, route: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={route} element={ui} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.strings = flattenStrings(translations.chat);
  mocks.getSharedSkill.mockResolvedValue(skill);
  mocks.fetchSkillVersions.mockResolvedValue([]);
  mocks.getSharedPrompt.mockResolvedValue(prompt);
  mocks.fetchPromptVersions.mockResolvedValue([]);
  mocks.sharePrompt.mockResolvedValue('/prompt/prompt-1');
  mocks.shareSkill.mockResolvedValue({ skill, shareUrl: '/skill/skill-1' });
  mocks.fetchPrompts.mockResolvedValue(undefined);
  mocks.copyToClipboard.mockResolvedValue(undefined);
  mocks.store.prompts = [
    {
      id: 'prompt-1',
      title: 'My prompt',
      content: 'Prompt body',
      userId: 'user-1',
      isShared: false,
      createdAt: '2024-01-01T00:00:00Z',
      updatedAt: '2024-01-01T00:00:00Z'
    }
  ];
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

window.HTMLElement.prototype.scrollIntoView = vi.fn();

describe('shared skill author label', () => {
  it('shows a non-empty author label', async () => {
    renderRoute(<SkillSharePage />, '/skill/skill-1', '/skill/:id');

    expect(await screen.findByText('By Ada Lovelace')).not.toBeNull();
    expect(screen.getByText(/v1/)).not.toBeNull();
  });

  it.each([undefined, null, '', '   '])(
    'hides an empty author label (%p)',
    async (authorLabel) => {
      mocks.getSharedSkill.mockResolvedValue({ ...skill, authorLabel });
      renderRoute(<SkillSharePage />, '/skill/skill-1', '/skill/:id');

      await screen.findByRole('heading', { name: 'Shared skill' });
      expect(screen.queryByText(/^By /)).toBeNull();
      expect(screen.getByText('v1')).not.toBeNull();
    }
  );
});

describe('shared prompt author label', () => {
  function renderPrompt() {
    return renderRoute(
      <ChainlitContext.Provider value={apiClient}>
        <PromptSharePage />
      </ChainlitContext.Provider>,
      '/prompt/prompt-1',
      '/prompt/:id'
    );
  }

  it('shows a non-empty author label', async () => {
    renderPrompt();

    expect(await screen.findByText('By Ada Lovelace')).not.toBeNull();
  });

  it.each([undefined, null, '', '   '])(
    'hides an empty author label (%p)',
    async (authorLabel) => {
      mocks.getSharedPrompt.mockResolvedValue({ ...prompt, authorLabel });
      renderPrompt();

      await screen.findByRole('heading', { name: 'Shared prompt' });
      expect(screen.queryByText(/^By /)).toBeNull();
    }
  );
});

describe('author label sharing disclosure', () => {
  it.each([false, true])(
    'discloses the public label before and after skill sharing (shared=%p)',
    (isShared) => {
      render(
        <SkillShareDialog
          open
          skill={{
            ...skill,
            userId: 'user-1',
            createdAt: '2024-01-01T00:00:00Z',
            isShared,
            kind: 'skill',
            enabled: false
          }}
          onClose={() => undefined}
        />
      );

      expect(screen.getByText(DISCLOSURE_DEFAULT)).not.toBeNull();
    }
  );

  it('uses an English fallback when a locale lacks the prompt disclosure key', async () => {
    delete mocks.strings[DISCLOSURE_KEY];
    render(<PromptGalleryButton onSelect={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: 'Library' }));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Share Prompt' })
    );

    await waitFor(() => {
      expect(mocks.toastSuccess).toHaveBeenCalledWith(
        translations.chat.promptGallery.copied,
        { description: DISCLOSURE_DEFAULT }
      );
    });
  });
});
