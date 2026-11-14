import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import Home from '@/pages/Home';
import ThreadPage from '@/pages/Thread';

const state = vi.hoisted(() => ({
  unavailableThreadId: undefined as string | undefined,
  threadId: undefined as string | undefined,
  clear: vi.fn()
}));

vi.mock('@chainlit-ng/react-client', () => ({
  useChainlitStore: (selector: (state: any) => unknown) =>
    selector({ unavailableThreadId: state.unavailableThreadId }),
  useChatInteract: () => ({ clear: state.clear }),
  useChatMessages: () => ({ threadId: state.threadId }),
  useConfig: () => ({ config: { threadResumable: true } }),
  useSetChainlitState: () => () => {}
}));

vi.mock('pages/Page', () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>
}));
vi.mock('@/components/ReadOnlyThread', () => ({
  ReadOnlyThread: ({ id }: { id: string }) => <div>History {id}</div>
}));
vi.mock('@/components/AutoResumeThread', () => ({
  default: () => <div>Resume</div>
}));
vi.mock('@/components/Loader', () => ({
  Loader: () => <div>Loading</div>
}));
vi.mock('@/components/chat', () => ({
  default: () => <div>Chat input</div>
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/thread/:id?" element={<ThreadPage />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('thread unavailable chat profile fallback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    state.unavailableThreadId = undefined;
    state.threadId = undefined;
  });

  it('shows read-only history for the unavailable thread', () => {
    state.unavailableThreadId = 'old';
    renderAt('/thread/old');
    expect(screen.getByText('History old')).toBeInTheDocument();
    expect(screen.queryByText('Resume')).not.toBeInTheDocument();
    expect(screen.queryByText('Loading')).not.toBeInTheDocument();
    expect(screen.queryByText('Chat input')).not.toBeInTheDocument();
  });

  it('continues to resume other available threads', () => {
    state.unavailableThreadId = 'another';
    renderAt('/thread/old');
    expect(screen.getByText('Resume')).toBeInTheDocument();
    expect(screen.getByText('Loading')).toBeInTheDocument();
    expect(screen.queryByText('History old')).not.toBeInTheDocument();
  });

  it('keeps the normal resumed chat writable', () => {
    state.threadId = 'old';
    renderAt('/thread/old');
    expect(screen.getByText('Chat input')).toBeInTheDocument();
    expect(screen.queryByText('History old')).not.toBeInTheDocument();
  });

  it('clears the unavailable session on the optional route without an id', () => {
    state.unavailableThreadId = 'old';
    renderAt('/thread');
    expect(state.clear).toHaveBeenCalledTimes(1);
  });

  it('clears the unavailable session when returning home', () => {
    state.unavailableThreadId = 'old';
    renderAt('/');
    expect(state.clear).toHaveBeenCalledTimes(1);
  });

  it('does not clear a normal session when returning home', () => {
    renderAt('/');
    expect(state.clear).not.toHaveBeenCalled();
  });
});
