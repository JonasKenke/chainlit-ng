import type { IApproval, IQuestion } from '@chainlit-ng/react-client';
import { fireEvent, render, screen } from '@testing-library/react';
import { StrictMode } from 'react';
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams
} from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import AutoResumeThread from '@/components/AutoResumeThread';

import { confirmPendingNavigation } from '@/hooks/usePendingInteraction';

const mocks = vi.hoisted(() => ({
  config: {
    threadResumable: true,
    dataPersistence: true
  } as { threadResumable: boolean; dataPersistence: boolean },
  clear: vi.fn(),
  setIdToResume: vi.fn(),
  threadId: 'thread-a' as string | undefined,
  idToResume: undefined as string | undefined,
  pendingState: {
    approvals: [] as IApproval[],
    questions: [] as IQuestion[]
  }
}));

vi.mock('@chainlit-ng/react-client', () => ({
  useConfig: () => ({ config: mocks.config }),
  useChatInteract: () => ({
    clear: mocks.clear,
    setIdToResume: mocks.setIdToResume
  }),
  useChatSession: () => ({
    session: undefined,
    idToResume: mocks.idToResume
  }),
  useChatMessages: () => ({ threadId: mocks.threadId }),
  useChainlitState: () => [undefined, vi.fn()],
  useChainlitStore: (selector: (state: typeof mocks.pendingState) => unknown) =>
    selector(mocks.pendingState)
}));

vi.mock('@/components/i18n', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      options?.defaultValue ?? key
  })
}));

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

const pendingApproval: IApproval = {
  id: 'approval-1',
  stepId: 'step-1',
  threadId: 'thread-a',
  tool: 'send_email',
  status: 'pending',
  live: true
};

const pendingQuestion: IQuestion = {
  id: 'question-1',
  stepId: 'step-1',
  threadId: 'thread-a',
  status: 'pending',
  prompt: 'Continue?',
  options: [],
  allowFreeText: false,
  live: true
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="path">{location.pathname}</output>;
}

function RouteControls() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate('/thread/thread-b')}>
        go-b
      </button>
      <button type="button" onClick={() => navigate('/')}>
        go-home
      </button>
    </>
  );
}

function RoutedAutoResume() {
  const { id } = useParams();
  return <AutoResumeThread id={id!} />;
}

function renderHarness(initial = '/thread/thread-a', strict = false) {
  const tree = (
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route
          path="/thread/:id"
          element={
            <>
              <RoutedAutoResume />
              <RouteControls />
              <LocationProbe />
            </>
          }
        />
        <Route
          path="/"
          element={
            <>
              <RouteControls />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>
  );

  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

function path() {
  return screen.getByTestId('path').textContent;
}

describe('AutoResumeThread pending interaction guard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.config = { threadResumable: true, dataPersistence: true };
    mocks.threadId = 'thread-a';
    mocks.idToResume = undefined;
    mocks.pendingState.approvals = [];
    mocks.pendingState.questions = [];
  });

  it('resumes directly without a pending interaction', () => {
    renderHarness();

    fireEvent.click(screen.getByText('go-b'));

    expect(
      screen.queryByText('Pending approval or question')
    ).not.toBeInTheDocument();
    expect(mocks.clear).toHaveBeenCalledTimes(1);
    expect(mocks.setIdToResume).toHaveBeenCalledWith('thread-b');
  });

  it.each([
    ['approval', { approvals: [pendingApproval] }],
    ['question', { questions: [pendingQuestion] }]
  ])('blocks a route-driven resume with a pending %s', (_kind, pending) => {
    mocks.pendingState = { ...mocks.pendingState, ...pending };
    renderHarness();

    fireEvent.click(screen.getByText('go-b'));

    expect(
      screen.getByText('Pending approval or question')
    ).toBeInTheDocument();
    expect(mocks.clear).not.toHaveBeenCalled();
    expect(mocks.setIdToResume).not.toHaveBeenCalled();
    expect(path()).toBe('/thread/thread-b');
  });

  it('returns to the current thread when cancelling without clearing', () => {
    mocks.pendingState.approvals = [pendingApproval];
    renderHarness();

    fireEvent.click(screen.getByText('go-b'));
    fireEvent.click(screen.getByText('Cancel'));

    expect(path()).toBe('/thread/thread-a');
    expect(mocks.clear).not.toHaveBeenCalled();
    expect(mocks.setIdToResume).not.toHaveBeenCalled();
  });

  it('cancels with Escape without clearing', () => {
    mocks.pendingState.approvals = [pendingApproval];
    renderHarness();

    fireEvent.click(screen.getByText('go-b'));
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(path()).toBe('/thread/thread-a');
    expect(mocks.clear).not.toHaveBeenCalled();
    expect(mocks.setIdToResume).not.toHaveBeenCalled();
  });

  it('returns to / when there is no current thread', () => {
    mocks.threadId = undefined;
    mocks.pendingState.questions = [pendingQuestion];
    renderHarness('/thread/thread-b');

    fireEvent.click(screen.getByText('Cancel'));

    expect(path()).toBe('/');
    expect(mocks.clear).not.toHaveBeenCalled();
    expect(mocks.setIdToResume).not.toHaveBeenCalled();
  });

  it('resumes and navigates home after confirming when persistence is off', () => {
    mocks.config = { threadResumable: true, dataPersistence: false };
    mocks.pendingState.approvals = [pendingApproval];
    renderHarness();

    fireEvent.click(screen.getByText('go-b'));
    fireEvent.click(screen.getByText('Switch chat'));

    expect(mocks.clear).toHaveBeenCalledTimes(1);
    expect(mocks.setIdToResume).toHaveBeenCalledWith('thread-b');
    expect(path()).toBe('/');
  });

  it('skips the warning for a navigation already confirmed by the click guard', () => {
    mocks.pendingState.approvals = [pendingApproval];
    confirmPendingNavigation('thread-b');
    renderHarness();

    fireEvent.click(screen.getByText('go-b'));

    expect(
      screen.queryByText('Pending approval or question')
    ).not.toBeInTheDocument();
    expect(mocks.clear).toHaveBeenCalledTimes(1);
    expect(mocks.setIdToResume).toHaveBeenCalledWith('thread-b');
  });

  it('consumes a confirmed navigation once when mounted in StrictMode', () => {
    mocks.pendingState.approvals = [pendingApproval];
    confirmPendingNavigation('thread-b');
    renderHarness('/thread/thread-b', true);

    expect(
      screen.queryByText('Pending approval or question')
    ).not.toBeInTheDocument();
    expect(mocks.clear).toHaveBeenCalledTimes(1);
    expect(mocks.setIdToResume).toHaveBeenCalledTimes(1);
    expect(mocks.setIdToResume).toHaveBeenCalledWith('thread-b');
  });
});
