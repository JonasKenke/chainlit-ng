import type {
  ISharedSkill,
  ISkillVersionSummary
} from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import SkillSharePage from '@/pages/SkillShare';

const navigateMock = vi.hoisted(() => vi.fn());
const getSharedSkillMock = vi.hoisted(() => vi.fn());
const addSharedSkillMock = vi.hoisted(() => vi.fn());
const fetchSkillVersionsMock = vi.hoisted(() => vi.fn());
const fetchSkillVersionMock = vi.hoisted(() => vi.fn());
const useAuthMock = vi.hoisted(() => vi.fn());
const configState = vi.hoisted(() => {
  const config: {
    skillsSharing?: boolean;
    features?: { skills?: boolean; latex?: boolean };
    skillsLibrary?: boolean;
  } = { skillsSharing: true, features: { skills: true } };
  return { config };
});

const sharedSkill: ISharedSkill = {
  id: 'shared-skill',
  title: 'Release notes',
  description: 'Draft release notes',
  content: '# Release notes\n\nList the changes.',
  version: 3,
  currentVersionId: 'sv3',
  contentHash: 'sha256:head',
  updatedAt: '2026-03-01T00:00:00Z',
  sharedAt: '2026-03-01T00:00:00Z',
  authorLabel: 'Chainlit user'
};

const versions: ISkillVersionSummary[] = [
  {
    id: 'sv3',
    promptId: 'shared-skill',
    kind: 'skill',
    version: 3,
    title: 'Release notes',
    description: '',
    contentHash: 'sha256:head',
    createdAt: '2026-03-01T00:00:00Z'
  },
  {
    id: 'sv2',
    promptId: 'shared-skill',
    kind: 'skill',
    version: 2,
    title: 'Release notes',
    description: '',
    contentHash: 'sha256:older',
    createdAt: '2026-02-01T00:00:00Z'
  }
];

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
  useParams: () => ({ id: 'shared-skill' })
}));

vi.mock('@/components/i18n/Translator', () => ({
  useTranslation: () => ({ t: (key: string) => key })
}));

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/components/Markdown', () => ({
  Markdown: ({
    children,
    allowHtml
  }: {
    children: string;
    allowHtml: boolean;
  }) => <pre data-allow-html={String(allowHtml)}>{children}</pre>
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
  useAuth: useAuthMock,
  useConfig: () => ({ config: configState.config }),
  useSkills: () => ({
    getSharedSkill: getSharedSkillMock,
    addSharedSkill: addSharedSkillMock,
    fetchSkillVersions: fetchSkillVersionsMock,
    fetchSkillVersion: fetchSkillVersionMock
  })
}));

vi.mock('pages/Page', () => ({
  default: ({ children }: { children?: ReactNode }) => <>{children}</>
}));

const renderPage = () => render(<SkillSharePage />);

describe('SkillSharePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    configState.config = { skillsSharing: true, features: { skills: true } };
    getSharedSkillMock.mockResolvedValue({ ...sharedSkill });
    addSharedSkillMock.mockResolvedValue({ id: 'copy' });
    fetchSkillVersionsMock.mockResolvedValue(versions);
    fetchSkillVersionMock.mockResolvedValue({
      ...versions[1],
      content: 'Older body'
    });
    useAuthMock.mockReturnValue({
      user: { id: 'u1' },
      data: { requireLogin: false },
      isAuthenticated: true
    });
  });

  it('previews a shared skill with sanitized Markdown and no owner id', async () => {
    renderPage();

    expect(await screen.findByText('Release notes')).toBeInTheDocument();
    expect(screen.getByText('Draft release notes')).toBeInTheDocument();
    const body = screen.getByText(/List the changes/);
    expect(body.closest('pre')).toHaveAttribute('data-allow-html', 'false');
    expect(document.body.textContent).not.toContain('u1');
  });

  it('hides the version selector when sharing is unavailable', async () => {
    configState.config = { features: { skills: true } };
    renderPage();

    await screen.findByText('Release notes');
    expect(screen.queryByLabelText('chat.skills.share.version')).toBeNull();
    expect(fetchSkillVersionsMock).not.toHaveBeenCalled();
  });

  it('previews an older version and pins it when adding', async () => {
    renderPage();

    await screen.findByLabelText('chat.skills.share.version');
    fireEvent.change(screen.getByLabelText('chat.skills.share.version'), {
      target: { value: 'sv2' }
    });

    await waitFor(() =>
      expect(fetchSkillVersionMock).toHaveBeenCalledWith(
        'shared-skill',
        'sv2',
        { shared: true }
      )
    );
    expect(await screen.findByText('Older body')).toBeInTheDocument();
    expect(screen.getByTestId('skill-share-add')).toHaveTextContent(
      'chat.skills.share.addVersion'
    );

    fireEvent.click(screen.getByTestId('skill-share-add'));
    await waitFor(() =>
      expect(addSharedSkillMock).toHaveBeenCalledWith('shared-skill', 'sv2')
    );
  });

  it('adds the head as a disabled copy and explains the review step', async () => {
    renderPage();
    await screen.findByText('Release notes');

    expect(screen.getByTestId('skill-share-disabled-note')).toHaveTextContent(
      'chat.skills.share.disabledNote'
    );
    fireEvent.click(screen.getByTestId('skill-share-add'));

    await waitFor(() =>
      expect(addSharedSkillMock).toHaveBeenCalledWith('shared-skill')
    );
    expect(await screen.findByTestId('skill-share-added')).toHaveTextContent(
      'chat.skills.share.addedReview'
    );
    expect(screen.getByTestId('skill-share-add')).toHaveTextContent(
      'chat.skills.share.openLibrary'
    );

    fireEvent.click(screen.getByTestId('skill-share-add'));
    expect(navigateMock).toHaveBeenCalledWith('/');
  });

  it('offers to open the existing copy when it was already added', async () => {
    addSharedSkillMock.mockRejectedValue({
      status: 409,
      detail: { code: 'already_added', message: 'already' }
    });
    renderPage();
    await screen.findByText('Release notes');

    fireEvent.click(screen.getByTestId('skill-share-add'));

    expect(
      await screen.findByTestId('skill-share-already-added')
    ).toHaveTextContent('chat.skills.share.alreadyAdded');
    expect(screen.getByTestId('skill-share-add')).toHaveTextContent(
      'chat.skills.share.openCopy'
    );
    fireEvent.click(screen.getByTestId('skill-share-add'));
    expect(navigateMock).toHaveBeenCalledWith('/');
  });

  it('blocks adding when the name is taken', async () => {
    addSharedSkillMock.mockRejectedValue({
      status: 409,
      detail: { code: 'name_taken', message: 'taken' }
    });
    renderPage();
    await screen.findByText('Release notes');

    fireEvent.click(screen.getByTestId('skill-share-add'));

    expect(
      await screen.findByTestId('skill-share-name-conflict')
    ).toHaveTextContent('chat.skills.share.nameTaken');
    expect(screen.getByTestId('skill-share-add')).toBeDisabled();
  });

  it('shows a not-found state for an unshared or deleted skill', async () => {
    getSharedSkillMock.mockRejectedValue({ status: 404, detail: 'missing' });
    renderPage();

    expect(
      await screen.findByText('chat.skills.share.notFound')
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.skills.share.goHome' })
    );
    expect(navigateMock).toHaveBeenCalledWith('/');
  });

  it('reports an unavailable library instead of an add button', async () => {
    configState.config = { features: { skills: false } };
    renderPage();
    await screen.findByText('Release notes');

    expect(
      screen.getByText('chat.skills.share.unavailable')
    ).toBeInTheDocument();
    expect(screen.queryByTestId('skill-share-add')).toBeNull();
  });
});
