import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { IPrompt, IScheduledTask, useConfig } from '@chainlit/react-client';

import { PromptSchedulingButton } from '@/components/chat/MessageComposer/PromptSchedulingButton';

const clientState = vi.hoisted(() => ({
  prompts: [] as any[],
  chatSettingsValue: { model: '🔷 Gemini 3.5 Flash' }
}));

const fetchScheduledTasks = vi.fn().mockResolvedValue([]);
const createScheduledTask = vi.fn();
const updateScheduledTask = vi.fn();
const deleteScheduledTask = vi.fn();
const runScheduledTask = vi.fn();
const fetchPrompts = vi.fn().mockResolvedValue([]);

vi.mock('@/components/i18n/Translator', () => ({
  default: ({ path }: { path: string }) => path,
  useTranslation: () => ({ t: (key: string) => key })
}));

vi.mock('@chainlit/react-client', () => ({
  useConfig: vi.fn(),
  usePromptGallery: () => ({ fetchPrompts }),
  useScheduledTasks: () => ({
    tasks: currentTasks,
    fetchScheduledTasks,
    createScheduledTask,
    updateScheduledTask,
    deleteScheduledTask,
    runScheduledTask
  }),
  useChainlitStore: (selector: (state: typeof clientState) => unknown) =>
    selector(clientState)
}));

global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
window.HTMLElement.prototype.scrollIntoView = vi.fn();

const task: IScheduledTask = {
  id: 'task-1',
  userId: 'user-1',
  userIdentifier: 'user@example.com',
  prompt: 'Existing prompt',
  chatProfile: null,
  model: '🔷 Gemini 3.5 Flash',
  cron: '0 9 * * *',
  timezone: 'UTC',
  enabled: true,
  afterHooks: ['Email'],
  nextRunAt: '2030-01-01T09:00:00Z',
  manualRunRequestedAt: null,
  lastRunAt: null,
  lastStatus: null,
  lastError: null,
  lastThreadId: null,
  leaseId: null,
  leaseUntil: null,
  createdAt: '2029-01-01T00:00:00Z',
  updatedAt: '2029-01-01T00:00:00Z'
};

let currentTasks: IScheduledTask[] = [];

const renderComponent = (prompts: IPrompt[] = []) => {
  clientState.prompts = prompts;
  return render(<PromptSchedulingButton />);
};

describe('PromptSchedulingButton', () => {
  beforeEach(() => {
    currentTasks = [];
    vi.clearAllMocks();
    (useConfig as ReturnType<typeof vi.fn>).mockReturnValue({
      config: {
        promptScheduling: true,
        promptGallery: true,
        scheduledTaskModels: ['🔷 Gemini 3.5 Flash', '⚖️ GPT 5'],
        chatProfiles: [{ name: 'fast', display_name: 'Fast', default: false }],
        scheduledTaskAfterHooks: [{ name: 'Email', default: true }]
      }
    });
    createScheduledTask.mockResolvedValue(task);
    updateScheduledTask.mockResolvedValue(task);
    deleteScheduledTask.mockResolvedValue({ success: true });
    runScheduledTask.mockResolvedValue(task);
  });

  it('hides the clock when scheduling is unavailable', () => {
    (useConfig as ReturnType<typeof vi.fn>).mockReturnValue({
      config: { promptScheduling: false }
    });
    const { container } = renderComponent();
    expect(container.firstChild).toBeNull();
  });

  it('creates a schedule with a prompt copied from the gallery', async () => {
    const prompt: IPrompt = {
      id: 'prompt-1',
      title: 'Summarise',
      content: 'Summarise this report',
      userId: 'user-1',
      isShared: false,
      createdAt: '',
      updatedAt: ''
    };
    renderComponent([prompt]);

    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptScheduling.button' })
    );
    await waitFor(() => expect(fetchScheduledTasks).toHaveBeenCalled());
    fireEvent.click(screen.getByText('chat.promptScheduling.new'));
    fireEvent.change(
      screen.getByLabelText('chat.promptScheduling.form.prompt'),
      {
        target: { value: 'draft' }
      }
    );
    fireEvent.click(
      screen.getByText('chat.promptScheduling.form.chooseGallery')
    );
    fireEvent.click(screen.getByRole('button', { name: /Summarise/ }));

    expect(
      screen.getByLabelText('chat.promptScheduling.form.prompt')
    ).toHaveValue('Summarise this report');
    fireEvent.click(screen.getByText('common.actions.submit'));

    await waitFor(() =>
      expect(createScheduledTask).toHaveBeenCalledWith(
        expect.objectContaining({
          prompt: 'Summarise this report',
          model: '🔷 Gemini 3.5 Flash',
          cron: '0 9 * * *',
          afterHooks: ['Email']
        })
      )
    );
  });

  it('creates a custom hourly schedule without exposing cron syntax', async () => {
    renderComponent();
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptScheduling.button' })
    );
    fireEvent.click(screen.getByText('chat.promptScheduling.new'));
    fireEvent.change(
      screen.getByLabelText('chat.promptScheduling.form.prompt'),
      { target: { value: 'hourly prompt' } }
    );
    fireEvent.change(
      screen.getByLabelText('chat.promptScheduling.form.recurrence'),
      { target: { value: 'custom' } }
    );
    expect(
      screen.getByLabelText('chat.promptScheduling.form.time')
    ).toHaveClass('[color-scheme:light]', 'dark:[color-scheme:dark]');
    fireEvent.change(
      screen.getByLabelText('chat.promptScheduling.form.customFrequency'),
      { target: { value: 'hours' } }
    );
    fireEvent.change(
      screen.getByLabelText('chat.promptScheduling.form.interval'),
      { target: { value: '3' } }
    );
    expect(
      screen.queryByText('chat.promptScheduling.form.advanced')
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('common.actions.submit'));

    await waitFor(() =>
      expect(createScheduledTask).toHaveBeenCalledWith(
        expect.objectContaining({
          prompt: 'hourly prompt',
          cron: '0 */3 * * *'
        })
      )
    );
  });

  it('loads an existing custom time when editing a schedule', async () => {
    currentTasks = [{ ...task, cron: '10 11 * * *' }];
    renderComponent();
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptScheduling.button' })
    );
    await waitFor(() => screen.getByText('Existing prompt'));
    fireEvent.click(
      screen.getByRole('button', {
        name: 'chat.promptScheduling.actions.edit'
      })
    );

    expect(
      screen.getByLabelText('chat.promptScheduling.form.time')
    ).toHaveValue('11:10');
  });

  it('runs and pauses an existing schedule', async () => {
    currentTasks = [task];
    renderComponent();
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptScheduling.button' })
    );
    await waitFor(() => screen.getByText('Existing prompt'));
    expect(
      screen.getByText(/chat\.promptScheduling\.overview\.dailyAt/)
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText('chat.promptScheduling.actions.runNow'));
    expect(runScheduledTask).toHaveBeenCalledWith('task-1');
    fireEvent.click(
      screen.getByRole('button', {
        name: 'chat.promptScheduling.actions.pause'
      })
    );
    expect(updateScheduledTask).toHaveBeenCalledWith('task-1', {
      enabled: false
    });
  });

  it('shows removed hooks as unavailable and filters them on save', async () => {
    currentTasks = [{ ...task, afterHooks: ['Removed'] }];
    renderComponent();
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptScheduling.button' })
    );
    await waitFor(() => screen.getByText('Existing prompt'));
    fireEvent.click(
      screen.getByRole('button', {
        name: 'chat.promptScheduling.actions.edit'
      })
    );
    expect(
      screen.getByText('Removed (chat.promptScheduling.form.unavailable)')
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText('common.actions.submit'));
    await waitFor(() =>
      expect(updateScheduledTask).toHaveBeenCalledWith(
        'task-1',
        expect.objectContaining({ afterHooks: [] })
      )
    );
  });

  it('translates completed run status', async () => {
    currentTasks = [{ ...task, lastStatus: 'success' }];
    renderComponent();
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptScheduling.button' })
    );
    await waitFor(() => screen.getByText('Existing prompt'));
    expect(screen.getByRole('status')).toHaveTextContent(
      'chat.promptScheduling.status.success'
    );
  });

  it('renders API validation errors inline', async () => {
    createScheduledTask.mockRejectedValue({ detail: 'Invalid timezone' });
    renderComponent();
    fireEvent.click(
      screen.getByRole('button', { name: 'chat.promptScheduling.button' })
    );
    fireEvent.click(screen.getByText('chat.promptScheduling.new'));
    fireEvent.change(
      screen.getByLabelText('chat.promptScheduling.form.prompt'),
      {
        target: { value: 'prompt' }
      }
    );
    fireEvent.click(screen.getByText('common.actions.submit'));
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('Invalid timezone')
    );
  });
});
