import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ChainlitAPI,
  ChainlitContext,
  ClientError,
  type IMcp,
  getChainlitStore
} from '@chainlit/react-client';

import { McpAddForm } from '@/components/chat/MessageComposer/Mcp/AddForm';
import { McpList } from '@/components/chat/MessageComposer/Mcp/List';

const toastPromise = vi.hoisted(() => vi.fn());

vi.mock('sonner', () => ({
  toast: {
    promise: toastPromise,
    error: vi.fn(),
    success: vi.fn()
  }
}));

vi.mock('@/components/i18n/Translator', () => {
  const t = (key: string, options?: { host?: string }) =>
    options?.host ? `${key}:${options.host}` : key;
  return {
    default: ({ path }: { path: string }) => path,
    useTranslation: () => ({ t })
  };
});

// Radix Select needs pointer events that jsdom does not implement; the form
// only needs a controllable native select for the transport.
vi.mock('@/components/ui/select', async () => {
  const React = await import('react');
  const SelectItem = ({ value, children }: any) => (
    <option value={value}>{children}</option>
  );
  const SelectContent = ({ children }: any) => <>{children}</>;
  return {
    Select: ({ value, onValueChange, disabled, children }: any) => {
      const content = React.Children.toArray(children).find(
        (child: any) => child?.type === SelectContent
      ) as any;
      return (
        <select
          aria-label="transport"
          value={value}
          disabled={disabled}
          onChange={(event) => onValueChange(event.target.value)}
        >
          {content?.props.children}
        </select>
      );
    },
    SelectTrigger: ({ children }: any) => <>{children}</>,
    SelectValue: () => null,
    SelectContent,
    SelectItem
  };
});

global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};

const AUTHORIZATION_URL = 'https://auth.example.com/authorize?state=abc';

function setupMcps(mcps: IMcp[]) {
  const client = new ChainlitAPI('http://localhost:8000', 'webapp');
  const store = getChainlitStore(client);
  store.setState({ sessionId: 'session-1', mcps });
  return { client, store };
}

const pendingMcp: IMcp = {
  name: 'stripe',
  tools: [],
  status: 'connecting',
  isUserProvided: true,
  clientType: 'streamable-http',
  url: 'https://mcp.example.com/mcp',
  useOAuth: true,
  authorizationUrl: AUTHORIZATION_URL
};

describe('MCP OAuth in the connect form', () => {
  beforeEach(() => {
    // sonner consumes the promise; the mock has to swallow the rejection too.
    toastPromise
      .mockReset()
      .mockImplementation((promise: Promise<unknown>) =>
        promise.catch(() => undefined)
      );
    vi.spyOn(window, 'open').mockImplementation(() => null);
  });

  it('shows the OAuth checkbox only for streamable HTTP', () => {
    const { client } = setupMcps([]);
    render(
      <ChainlitContext.Provider value={client}>
        <McpAddForm
          servers={[]}
          userServersEnabled
          onSuccess={vi.fn()}
          onCancel={vi.fn()}
        />
      </ChainlitContext.Provider>
    );

    // The default transport is SSE.
    expect(screen.queryByRole('checkbox')).toBeNull();

    const transport = screen.getByLabelText('transport');
    fireEvent.change(transport, { target: { value: 'streamable-http' } });
    expect(screen.getByRole('checkbox')).toBeInTheDocument();

    fireEvent.change(transport, { target: { value: 'sse' } });
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('sends useOAuth and keeps a 202 authorization URL pending', async () => {
    const { client, store } = setupMcps([]);
    const connectUserMcp = vi
      .spyOn(client, 'connectUserMcp')
      .mockResolvedValue({
        status: 'authorization_required',
        url: AUTHORIZATION_URL,
        mcp: {
          name: 'stripe',
          tools: [],
          status: 'connecting',
          isUserProvided: true,
          clientType: 'streamable-http',
          url: 'https://mcp.example.com/mcp'
        }
      });
    const onSuccess = vi.fn();

    render(
      <ChainlitContext.Provider value={client}>
        <McpAddForm
          servers={[]}
          userServersEnabled
          onSuccess={onSuccess}
          onCancel={vi.fn()}
        />
      </ChainlitContext.Provider>
    );

    fireEvent.change(screen.getByLabelText('transport'), {
      target: { value: 'streamable-http' }
    });
    fireEvent.change(screen.getByLabelText(/chat.mcp.form.name/), {
      target: { value: 'stripe' }
    });
    fireEvent.change(screen.getByLabelText(/chat.mcp.form.url/), {
      target: { value: 'https://mcp.example.com/mcp' }
    });
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByText('chat.mcp.form.add'));

    await waitFor(() => expect(onSuccess).toHaveBeenCalled());

    expect(connectUserMcp).toHaveBeenCalledWith(
      'session-1',
      'stripe',
      'streamable-http',
      'https://mcp.example.com/mcp',
      undefined,
      true
    );
    expect(store.getState().mcps?.[0]).toMatchObject({
      name: 'stripe',
      status: 'connecting',
      useOAuth: true,
      authorizationUrl: AUTHORIZATION_URL
    });
  });

  it('shows the backend 400 detail instead of a generic message', async () => {
    const { client } = setupMcps([]);
    const detail =
      'OAuth is not supported for SSE MCP connections; use streamable HTTP';
    vi.spyOn(client, 'connectUserMcp').mockRejectedValue(
      new ClientError('Bad Request', 400, detail)
    );

    render(
      <ChainlitContext.Provider value={client}>
        <McpAddForm
          servers={[]}
          userServersEnabled
          onSuccess={vi.fn()}
          onCancel={vi.fn()}
        />
      </ChainlitContext.Provider>
    );

    fireEvent.change(screen.getByLabelText('transport'), {
      target: { value: 'streamable-http' }
    });
    fireEvent.change(screen.getByLabelText(/chat.mcp.form.name/), {
      target: { value: 'sse-server' }
    });
    fireEvent.change(screen.getByLabelText(/chat.mcp.form.url/), {
      target: { value: 'https://mcp.example.com/sse' }
    });
    fireEvent.click(screen.getByText('chat.mcp.form.add'));

    await waitFor(() => expect(toastPromise).toHaveBeenCalled());

    const { error } = toastPromise.mock.calls[0][1] as {
      error: (err: unknown) => string;
    };
    expect(error(new ClientError('Bad Request', 400, detail))).toBe(detail);
  });
});

describe('MCP OAuth in the connected list', () => {
  beforeEach(() => {
    vi.spyOn(window, 'open').mockImplementation(() => null);
  });

  it('shows a sign-in link for a pending server and opens it on click', () => {
    const { client } = setupMcps([pendingMcp]);

    render(
      <ChainlitContext.Provider value={client}>
        <McpList onAddNewClick={vi.fn()} />
      </ChainlitContext.Provider>
    );

    const link = screen.getByRole('link');
    expect(link).toHaveTextContent('chat.mcp.list.signIn:auth.example.com');
    expect(link).toHaveAttribute('href', AUTHORIZATION_URL);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(window.open).not.toHaveBeenCalled();

    fireEvent.click(link);

    expect(window.open).toHaveBeenCalledWith(
      AUTHORIZATION_URL,
      '_blank',
      'noopener,noreferrer'
    );
  });

  it('removes a pending server and cancels the sign-in on the backend', async () => {
    const { client, store } = setupMcps([pendingMcp]);
    const disconnectMcp = vi
      .spyOn(client, 'disconnectMcp')
      .mockResolvedValue({ success: true });

    render(
      <ChainlitContext.Provider value={client}>
        <McpList onAddNewClick={vi.fn()} />
      </ChainlitContext.Provider>
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'chat.mcp.list.remove' })
    );
    fireEvent.click(screen.getByText('common.actions.confirm'));

    await waitFor(() =>
      expect(disconnectMcp).toHaveBeenCalledWith('session-1', 'stripe')
    );
    expect(store.getState().mcps).toHaveLength(0);
  });

  it('drops the sign-in link once connected', () => {
    const { client } = setupMcps([
      { ...pendingMcp, status: 'connected', authorizationUrl: undefined }
    ]);

    render(
      <ChainlitContext.Provider value={client}>
        <McpList onAddNewClick={vi.fn()} />
      </ChainlitContext.Provider>
    );

    expect(screen.queryByRole('link')).toBeNull();
  });

  it('shows a failed connection detail', () => {
    const { client } = setupMcps([
      {
        ...pendingMcp,
        status: 'failed',
        authorizationUrl: undefined,
        detail: 'The authorization server refused the sign-in'
      }
    ]);

    render(
      <ChainlitContext.Provider value={client}>
        <McpList onAddNewClick={vi.fn()} />
      </ChainlitContext.Provider>
    );

    expect(
      screen.getByText('The authorization server refused the sign-in')
    ).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('never renders a clickable link for a non-http authorization URL', () => {
    const { client } = setupMcps([
      { ...pendingMcp, authorizationUrl: 'javascript:alert(1)' }
    ]);

    render(
      <ChainlitContext.Provider value={client}>
        <McpList onAddNewClick={vi.fn()} />
      </ChainlitContext.Provider>
    );

    expect(screen.queryByRole('link')).toBeNull();
  });
});
