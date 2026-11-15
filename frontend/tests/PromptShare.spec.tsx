// ── imports after mocks ────────────────────────────────────────────────────
import { ChainlitContext } from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import PromptSharePage from '@/pages/PromptShare';

// ── hoisted mocks (must be defined before vi.mock factories run) ────────────
const navigateMock = vi.hoisted(() => vi.fn());
const addSharedPromptMock = vi.hoisted(() =>
  vi.fn().mockResolvedValue(undefined)
);
const getSharedPromptMock = vi.hoisted(() => vi.fn());
const fetchPromptVersionsMock = vi.hoisted(() => vi.fn());
const fetchPromptVersionMock = vi.hoisted(() => vi.fn());
const useAuthMock = vi.hoisted(() => vi.fn());
const configMock = vi.hoisted(() => ({
  config: { promptGallery: true } as Record<string, unknown>
}));

const sharedPrompt = {
  id: 'shared-abc',
  title: 'Cool Prompt',
  content: 'Do something cool',
  isShared: true,
  createdAt: '',
  updatedAt: '',
  version: 3,
  currentVersionId: 'v3',
  currentContentHash: 'sha256:head'
};

const versionSummaries = [
  {
    id: 'v3',
    promptId: 'shared-abc',
    kind: 'prompt',
    version: 3,
    title: 'Cool Prompt',
    description: '',
    contentHash: 'sha256:head',
    createdAt: '2024-03-01T00:00:00Z'
  },
  {
    id: 'v2',
    promptId: 'shared-abc',
    kind: 'prompt',
    version: 2,
    title: 'Cool Prompt',
    description: '',
    contentHash: 'sha256:older',
    createdAt: '2024-02-01T00:00:00Z'
  }
];

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useParams: () => ({ id: 'shared-abc' })
}));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({ t: (k: string) => k })
}));

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@chainlit-ng/react-client', async () => {
  const { createContext } = await import('react');
  return {
    ChainlitContext: createContext<unknown>(undefined),
    useAuth: useAuthMock,
    useConfig: () => configMock,
    usePromptGallery: () => ({ addSharedPrompt: addSharedPromptMock }),
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
  };
});

vi.mock('pages/Page', () => ({
  default: ({ children }: { children?: ReactNode }) => <>{children}</>
}));

const fakeApi = {
  getSharedPrompt: getSharedPromptMock,
  fetchPromptVersions: fetchPromptVersionsMock,
  fetchPromptVersion: fetchPromptVersionMock
};

const renderPage = () =>
  render(
    <ChainlitContext.Provider value={fakeApi}>
      <PromptSharePage />
    </ChainlitContext.Provider>
  );

const enableVersioning = () => {
  configMock.config = { promptGallery: true, promptVersioning: true };
};

describe('PromptSharePage', () => {
  beforeEach(() => {
    navigateMock.mockReset();
    addSharedPromptMock.mockReset().mockResolvedValue(undefined);
    getSharedPromptMock.mockReset().mockResolvedValue({ ...sharedPrompt });
    fetchPromptVersionsMock.mockReset().mockResolvedValue(versionSummaries);
    fetchPromptVersionMock.mockReset().mockResolvedValue({
      ...versionSummaries[1],
      content: 'Older body'
    });
    configMock.config = { promptGallery: true };
    useAuthMock.mockReturnValue({
      user: { id: 'u1' },
      data: { requireLogin: false },
      isAuthenticated: true
    });
  });

  it('shows the shared prompt title and content', async () => {
    renderPage();
    await waitFor(() => screen.getByText('Cool Prompt'));
    expect(screen.getByText('Do something cool')).toBeInTheDocument();
  });

  it('hides the version selector when versioning is unavailable', async () => {
    renderPage();
    await waitFor(() => screen.getByText('Cool Prompt'));
    expect(screen.queryByLabelText('chat.promptGallery.share.version')).toBe(
      null
    );
    expect(fetchPromptVersionsMock).not.toHaveBeenCalled();
  });

  it('hides the version selector when the versions route answers 501', async () => {
    enableVersioning();
    fetchPromptVersionsMock.mockRejectedValue({
      status: 501,
      detail: 'Prompt versioning is unavailable'
    });
    renderPage();
    await waitFor(() => screen.getByText('Cool Prompt'));
    await waitFor(() =>
      expect(fetchPromptVersionsMock).toHaveBeenCalledWith('shared-abc', {
        shared: true
      })
    );
    expect(screen.queryByLabelText('chat.promptGallery.share.version')).toBe(
      null
    );
  });

  it('navigates to / after adding the prompt', async () => {
    renderPage();
    await waitFor(() => screen.getByText('Cool Prompt'));
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptGallery.addToGallery' })
    );
    await waitFor(() =>
      expect(addSharedPromptMock).toHaveBeenCalledWith('shared-abc')
    );
    expect(navigateMock).toHaveBeenCalledWith('/');
  });

  it('does not expose the owner identity', async () => {
    renderPage();
    await waitFor(() => screen.getByText('Cool Prompt'));
    expect(document.body.textContent).not.toContain('u1');
  });

  it('previews a historical version and pins it when adding', async () => {
    enableVersioning();
    renderPage();
    await waitFor(() =>
      expect(screen.getByLabelText('chat.promptGallery.share.version'))
    );
    expect(fetchPromptVersionsMock).toHaveBeenCalledWith('shared-abc', {
      shared: true
    });

    fireEvent.change(
      screen.getByLabelText('chat.promptGallery.share.version'),
      {
        target: { value: 'v2' }
      }
    );

    await waitFor(() =>
      expect(fetchPromptVersionMock).toHaveBeenCalledWith('shared-abc', 'v2', {
        shared: true
      })
    );
    await waitFor(() =>
      expect(screen.getByTestId('prompt-share-content')).toHaveTextContent(
        'Older body'
      )
    );

    const button = screen.getByTestId('prompt-share-add');
    expect(button).toHaveTextContent('chat.promptGallery.share.addVersion');

    fireEvent.click(button);
    await waitFor(() =>
      expect(addSharedPromptMock).toHaveBeenCalledWith('shared-abc', 'v2')
    );
  });

  it('adds the current head when the latest version is selected', async () => {
    enableVersioning();
    renderPage();
    await waitFor(() =>
      expect(screen.getByLabelText('chat.promptGallery.share.version'))
    );
    fireEvent.click(screen.getByTestId('prompt-share-add'));
    await waitFor(() =>
      expect(addSharedPromptMock).toHaveBeenCalledWith('shared-abc')
    );
    expect(fetchPromptVersionMock).not.toHaveBeenCalled();
  });

  it('offers to open the existing copy when it was already added', async () => {
    addSharedPromptMock.mockRejectedValue({
      status: 409,
      detail: { code: 'already_added', message: 'already' }
    });
    renderPage();
    await waitFor(() => screen.getByText('Cool Prompt'));

    fireEvent.click(screen.getByTestId('prompt-share-add'));

    const openCopy = await screen.findByRole('button', {
      name: 'chat.promptGallery.share.openCopy'
    });
    expect(navigateMock).not.toHaveBeenCalledWith('/');

    fireEvent.click(openCopy);
    expect(navigateMock).toHaveBeenCalledWith('/');
    expect(addSharedPromptMock).toHaveBeenCalledTimes(1);
  });

  it('allows saving when login is not required', async () => {
    useAuthMock.mockReturnValue({
      user: null,
      data: { requireLogin: false },
      isAuthenticated: true
    });

    renderPage();
    await waitFor(() => screen.getByText('Cool Prompt'));
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptGallery.addToGallery' })
    );
    await waitFor(() =>
      expect(addSharedPromptMock).toHaveBeenCalledWith('shared-abc')
    );
    expect(navigateMock).toHaveBeenCalledWith('/');
  });
});
