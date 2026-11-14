import {
  ChainlitAPI,
  ChainlitContext,
  ClientError,
  type IMcp,
  getChainlitStore
} from '@chainlit-ng/react-client';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { McpLibraryButton } from '@/components/chat/MessageComposer/Mcp/Library';

const toastSuccess = vi.hoisted(() => vi.fn());
const toastError = vi.hoisted(() => vi.fn());

vi.mock('sonner', () => ({
  toast: {
    success: toastSuccess,
    error: toastError,
    promise: vi.fn()
  }
}));

vi.mock('@/components/i18n/Translator', () => ({
  default: ({ path }: { path: string }) => path,
  useTranslation: () => ({ t: (key: string) => key })
}));

// required for Radix UI
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
window.HTMLElement.prototype.scrollIntoView = vi.fn();

const connectedMcp: IMcp = {
  name: 'docs',
  tools: [{ name: 'search' }],
  prompts: [
    {
      name: 'standup',
      description: 'Daily standup',
      arguments: [{ name: 'team', required: true, description: 'Team name' }]
    },
    { name: 'plain', description: 'No arguments' }
  ],
  resources: [
    {
      uri: 'file:///readme.md',
      name: 'README',
      mimeType: 'text/markdown'
    }
  ],
  status: 'connected',
  type: 'streamable-http'
};

function setup(mcps: IMcp[] = [connectedMcp]) {
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const store = getChainlitStore(client);
  store.setState({ sessionId: 'session-1', mcps });
  const onInsertPrompt = vi.fn();
  render(
    <ChainlitContext.Provider value={client}>
      <McpLibraryButton onInsertPrompt={onInsertPrompt} />
    </ChainlitContext.Provider>
  );
  return { client, onInsertPrompt };
}

async function openLibrary() {
  fireEvent.click(screen.getByRole('button'));
  await screen.findByRole('dialog');
}

describe('MCP library', () => {
  beforeEach(() => {
    toastSuccess.mockReset();
    toastError.mockReset();
  });

  it('renders nothing when no connected server publishes prompts or resources', () => {
    const client = new ChainlitAPI('http://localhost:8000', 'webapp');
    getChainlitStore(client).setState({
      sessionId: 'session-1',
      mcps: [{ name: 'docs', tools: [], status: 'connected' }]
    });

    render(
      <ChainlitContext.Provider value={client}>
        <McpLibraryButton onInsertPrompt={vi.fn()} />
      </ChainlitContext.Provider>
    );

    expect(screen.queryByRole('button')).toBeNull();
  });

  it('inserts a prompt that takes no arguments', async () => {
    const { client, onInsertPrompt } = setup();
    const getPrompt = vi
      .spyOn(client, 'getMcpPrompt')
      .mockResolvedValue({ text: 'Rendered prompt' });
    await openLibrary();

    fireEvent.click(screen.getByText('plain'));

    await waitFor(() =>
      expect(getPrompt).toHaveBeenCalledWith('session-1', 'docs', 'plain', {})
    );
    expect(onInsertPrompt).toHaveBeenCalledWith('Rendered prompt');
    expect(toastSuccess).toHaveBeenCalled();
  });

  it('collects required arguments before invoking the prompt', async () => {
    const { client, onInsertPrompt } = setup();
    const getPrompt = vi
      .spyOn(client, 'getMcpPrompt')
      .mockResolvedValue({ text: 'Standup for core' });
    await openLibrary();

    fireEvent.click(screen.getByText('standup'));
    // Nothing is fetched until the form is submitted.
    expect(getPrompt).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('team *'), {
      target: { value: 'core' }
    });
    fireEvent.click(screen.getByText('chat.mcp.library.promptInsert'));

    await waitFor(() =>
      expect(getPrompt).toHaveBeenCalledWith('session-1', 'docs', 'standup', {
        team: 'core'
      })
    );
    expect(onInsertPrompt).toHaveBeenCalledWith('Standup for core');
  });

  it('reads a resource through the backend', async () => {
    const { client } = setup();
    const readResource = vi
      .spyOn(client, 'readMcpResource')
      .mockResolvedValue({ element: { id: 'element-1' } });
    await openLibrary();

    const resourcesTab = screen.getByText('chat.mcp.library.tabs.resources');
    fireEvent.mouseDown(resourcesTab);
    fireEvent.click(resourcesTab);
    fireEvent.click(await screen.findByText('README'));

    await waitFor(() =>
      expect(readResource).toHaveBeenCalledWith(
        'session-1',
        'docs',
        'file:///readme.md',
        'README'
      )
    );
    expect(toastSuccess).toHaveBeenCalled();
  });

  it('shows the backend detail when a prompt fails', async () => {
    const { client, onInsertPrompt } = setup();
    vi.spyOn(client, 'getMcpPrompt').mockRejectedValue(
      new ClientError('Request failed', 422, 'Missing required prompt argument')
    );
    await openLibrary();

    fireEvent.click(screen.getByText('plain'));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'Missing required prompt argument'
      )
    );
    expect(onInsertPrompt).not.toHaveBeenCalled();
  });
});
