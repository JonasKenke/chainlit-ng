import { cn } from '@/lib/utils';
import {
  ChatProfile,
  IPrompt,
  IScheduledTask,
  IScheduledTaskPayload,
  useChainlitStore,
  useConfig,
  usePromptGallery,
  useScheduledTasks
} from '@chainlit-ng/react-client';
import {
  Clock,
  ExternalLink,
  Pause,
  Pencil,
  Play,
  Plus,
  RotateCcw,
  Trash2
} from 'lucide-react';
import { useEffect, useState } from 'react';

import Translator, { useTranslation } from '@/components/i18n/Translator';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';

interface Props {
  disabled?: boolean;
}

interface FormState {
  prompt: string;
  chatProfile: string;
  model: string;
  cron: string;
  recurrence: Preset | 'custom';
  customFrequency: CustomFrequency;
  customInterval: number;
  customWeekday: string;
  customDay: number;
  customTime: string;
  afterHooks: string[];
}

const presetCrons = {
  daily: '0 9 * * *',
  weekdays: '0 9 * * 1-5',
  weekly: '0 9 * * 1',
  monthly: '0 9 1 * *'
};

type Preset = keyof typeof presetCrons;
type CustomFrequency = 'hours' | 'daily' | 'weekly' | 'monthly';
type CustomField =
  | 'customFrequency'
  | 'customInterval'
  | 'customWeekday'
  | 'customDay'
  | 'customTime';

const browserTimezone = () =>
  Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

const customDefaults = {
  customFrequency: 'daily' as CustomFrequency,
  customInterval: 1,
  customWeekday: '1',
  customDay: 1,
  customTime: '09:00'
};

const recurrenceForCron = (cron: string): Preset | 'custom' =>
  (Object.keys(presetCrons) as Preset[]).find(
    (key) => presetCrons[key] === cron
  ) || 'custom';

const buildCustomCron = (
  frequency: CustomFrequency,
  interval: number,
  weekday: string,
  day: number,
  time: string
) => {
  const [rawHour, rawMinute] = time.split(':').map(Number);
  const hour =
    Number.isInteger(rawHour) && rawHour >= 0 && rawHour <= 23 ? rawHour : 9;
  const minute =
    Number.isInteger(rawMinute) && rawMinute >= 0 && rawMinute <= 59
      ? rawMinute
      : 0;
  if (frequency === 'hours') {
    const safeInterval =
      Number.isInteger(interval) && interval > 0 && interval <= 23
        ? interval
        : 1;
    return `0 */${safeInterval} * * *`;
  }
  if (frequency === 'weekly') return `${minute} ${hour} * * ${weekday}`;
  if (frequency === 'monthly') {
    const safeDay = Number.isInteger(day) && day > 0 && day <= 31 ? day : 1;
    return `${minute} ${hour} ${safeDay} * *`;
  }
  return `${minute} ${hour} * * *`;
};

const customValuesFromCron = (cron: string) => {
  const [minute, hour, dayOfMonth, month, dayOfWeek] = cron.trim().split(/\s+/);
  const time =
    /^\d+$/.test(minute || '') && /^\d+$/.test(hour || '')
      ? `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
      : customDefaults.customTime;
  const hoursEvery = (hour || '').match(/^\*\/(\d+)$/);
  if (
    minute === '0' &&
    hoursEvery &&
    dayOfMonth === '*' &&
    month === '*' &&
    dayOfWeek === '*'
  ) {
    return {
      ...customDefaults,
      customFrequency: 'hours' as CustomFrequency,
      customInterval: Number(hoursEvery[1])
    };
  }
  if (dayOfMonth === '*' && month === '*' && /^\d$/.test(dayOfWeek || '')) {
    return {
      ...customDefaults,
      customFrequency: 'weekly' as CustomFrequency,
      customWeekday: dayOfWeek,
      customTime: time
    };
  }
  if (month === '*' && dayOfWeek === '*' && /^\d+$/.test(dayOfMonth || '')) {
    return {
      ...customDefaults,
      customFrequency: 'monthly' as CustomFrequency,
      customDay: Number(dayOfMonth),
      customTime: time
    };
  }
  if (dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return {
      ...customDefaults,
      customFrequency: 'daily' as CustomFrequency,
      customTime: time
    };
  }
  return customDefaults;
};

const emptyForm = (
  hooks: { name: string; default: boolean }[],
  model = ''
): FormState => ({
  prompt: '',
  chatProfile: '',
  model,
  cron: presetCrons.daily,
  recurrence: 'daily',
  ...customDefaults,
  afterHooks: hooks.filter((hook) => hook.default).map((hook) => hook.name)
});

const taskForm = (
  task: IScheduledTask,
  modelOptions: string[],
  fallbackModel = ''
): FormState => ({
  prompt: task.prompt,
  chatProfile: task.chatProfile || '',
  model:
    task.model && modelOptions.includes(task.model)
      ? task.model
      : fallbackModel,
  cron: task.cron,
  recurrence: recurrenceForCron(task.cron),
  ...customValuesFromCron(task.cron),
  afterHooks: task.afterHooks
});

const errorMessage = (error: unknown, fallback: string) => {
  if (error && typeof error === 'object') {
    const detail = (error as { detail?: string }).detail;
    if (detail) return detail;
    if (error instanceof Error && error.message) return error.message;
  }
  return fallback;
};

const displayDate = (value: string | null) =>
  value ? new Date(value).toLocaleString() : 'Never';

const statusLabel = (status: string, t: (key: string) => string) => {
  const keys: Record<string, string> = {
    success: 'chat.promptScheduling.status.success',
    failure: 'chat.promptScheduling.status.failure',
    success_with_warnings: 'chat.promptScheduling.status.successWithWarnings'
  };
  return keys[status] ? t(keys[status]) : status;
};

const timeLabel = (hour: string, minute: string) =>
  `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`;

const describeCron = (cron: string, t: (key: string) => string) => {
  const [minute, hour, dayOfMonth, month, dayOfWeek] = cron.trim().split(/\s+/);
  if (!minute || !hour || !dayOfMonth || !month || !dayOfWeek) {
    return t('chat.promptScheduling.overview.custom');
  }
  const hoursEvery = hour.match(/^\*\/(\d+)$/);
  if (
    minute === '0' &&
    hoursEvery &&
    dayOfMonth === '*' &&
    month === '*' &&
    dayOfWeek === '*'
  ) {
    return t('chat.promptScheduling.overview.everyHours').replace(
      '{interval}',
      hoursEvery[1]
    );
  }
  const minutesEvery = minute.match(/^\*\/(\d+)$/);
  if (
    minutesEvery &&
    hour === '*' &&
    dayOfMonth === '*' &&
    month === '*' &&
    dayOfWeek === '*'
  ) {
    return t('chat.promptScheduling.overview.everyMinutes').replace(
      '{interval}',
      minutesEvery[1]
    );
  }
  if (!/^\d+$/.test(minute) || !/^\d+$/.test(hour)) {
    return t('chat.promptScheduling.overview.custom');
  }
  const time = timeLabel(hour, minute);
  if (dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return t('chat.promptScheduling.overview.dailyAt').replace('{time}', time);
  }
  if (dayOfMonth === '*' && month === '*' && dayOfWeek === '1-5') {
    return t('chat.promptScheduling.overview.weekdaysAt').replace(
      '{time}',
      time
    );
  }
  if (dayOfMonth === '*' && month === '*' && /^\d$/.test(dayOfWeek)) {
    const weekdayKeys = [
      'sunday',
      'monday',
      'tuesday',
      'wednesday',
      'thursday',
      'friday',
      'saturday'
    ];
    const weekday = t(
      `chat.promptScheduling.weekdays.${weekdayKeys[Number(dayOfWeek)]}`
    );
    return t('chat.promptScheduling.overview.weeklyAt')
      .replace('{weekday}', weekday)
      .replace('{time}', time);
  }
  if (month === '*' && dayOfWeek === '*' && /^\d+$/.test(dayOfMonth)) {
    return t('chat.promptScheduling.overview.monthlyAt')
      .replace('{day}', dayOfMonth)
      .replace('{time}', time);
  }
  return t('chat.promptScheduling.overview.custom');
};

export function PromptSchedulingButton({ disabled = false }: Props) {
  const { config } = useConfig();
  const { t } = useTranslation();
  const {
    tasks,
    fetchScheduledTasks,
    createScheduledTask,
    updateScheduledTask,
    deleteScheduledTask,
    runScheduledTask
  } = useScheduledTasks();
  const prompts = useChainlitStore((state) => state.prompts);
  const chatSettings = useChainlitStore((state) => state.chatSettingsValue);
  const modelOptions = config?.scheduledTaskModels || [];
  const currentModel =
    typeof chatSettings.model === 'string' &&
    modelOptions.includes(chatSettings.model)
      ? chatSettings.model
      : modelOptions[0] || '';
  const { fetchPrompts } = usePromptGallery();
  const [open, setOpen] = useState(false);
  const [galleryOpen, setGalleryOpen] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<IScheduledTask | null>(null);
  const [form, setForm] = useState<FormState>(() => emptyForm([]));
  const [error, setError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<IScheduledTask | null>(null);
  const hooks = config?.scheduledTaskAfterHooks || [];
  const profiles = config?.chatProfiles || [];
  const showProfileSelector = profiles.length > 1;
  useEffect(() => {
    if (!open) return;
    fetchScheduledTasks().catch((reason) =>
      setError(errorMessage(reason, t('chat.promptScheduling.errors.load')))
    );
  }, [open, fetchScheduledTasks, t]);

  if (!config?.promptScheduling) return null;

  const beginCreate = () => {
    setEditing(null);
    setForm(emptyForm(hooks, currentModel));
    setError('');
    setFormOpen(true);
  };

  const beginEdit = (task: IScheduledTask) => {
    setEditing(task);
    setForm(taskForm(task, modelOptions, currentModel));
    setError('');
    setFormOpen(true);
  };

  const save = async () => {
    if (!form.prompt.trim() || !form.cron.trim()) {
      setError(t('chat.promptScheduling.errors.required'));
      return;
    }
    setError('');
    const payload: IScheduledTaskPayload = {
      prompt: form.prompt,
      cron: form.cron,
      timezone: browserTimezone(),
      afterHooks: form.afterHooks.filter((name) =>
        hooks.some((hook) => hook.name === name)
      )
    };
    if (showProfileSelector) {
      payload.chatProfile = form.chatProfile || null;
    }
    if (modelOptions.length > 0 && (modelOptions.length > 1 || !editing)) {
      payload.model = form.model || null;
    }
    try {
      if (editing) await updateScheduledTask(editing.id, payload);
      else await createScheduledTask(payload);
      setFormOpen(false);
      await fetchScheduledTasks();
    } catch (reason) {
      setError(errorMessage(reason, t('chat.promptScheduling.errors.save')));
    }
  };

  const toggle = async (task: IScheduledTask) => {
    try {
      await updateScheduledTask(task.id, { enabled: !task.enabled });
    } catch (reason) {
      setError(errorMessage(reason, t('chat.promptScheduling.errors.update')));
    }
  };

  const remove = async () => {
    if (!deleteTarget) return;
    try {
      await deleteScheduledTask(deleteTarget.id);
      setDeleteTarget(null);
    } catch (reason) {
      setError(errorMessage(reason, t('chat.promptScheduling.errors.delete')));
    }
  };

  const run = async (task: IScheduledTask) => {
    try {
      await runScheduledTask(task.id);
    } catch (reason) {
      setError(errorMessage(reason, t('chat.promptScheduling.errors.run')));
    }
  };

  const setFormField = <K extends keyof FormState>(
    key: K,
    value: FormState[K]
  ) => setForm((current) => ({ ...current, [key]: value }));

  const setCustomField = <K extends CustomField>(key: K, value: FormState[K]) =>
    setForm((current) => {
      const next = { ...current, recurrence: 'custom' as const, [key]: value };
      return {
        ...next,
        cron: buildCustomCron(
          next.customFrequency,
          next.customInterval,
          next.customWeekday,
          next.customDay,
          next.customTime
        )
      };
    });

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          setOpen(nextOpen);
          if (!nextOpen) setFormOpen(false);
        }}
      >
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                id="prompt-scheduling-open"
                variant="ghost"
                size="icon"
                className={cn(
                  'rounded-full hover:bg-muted',
                  open && 'bg-muted/50'
                )}
                disabled={disabled}
                aria-label={t('chat.promptScheduling.button')}
                onClick={() => setOpen(true)}
              >
                <Clock className="!size-6" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              <p>{t('chat.promptScheduling.button')}</p>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <DialogContent
          className="sm:max-w-2xl max-h-[85vh] overflow-y-auto"
          aria-describedby={undefined}
        >
          <DialogHeader className="flex flex-row items-center justify-between pr-8">
            <DialogTitle>
              {formOpen
                ? editing
                  ? t('chat.promptScheduling.edit')
                  : t('chat.promptScheduling.create')
                : t('chat.promptScheduling.title')}
            </DialogTitle>
            {!formOpen && (
              <Button size="sm" onClick={beginCreate}>
                <Plus className="h-4 w-4 mr-1" />
                {t('chat.promptScheduling.new')}
              </Button>
            )}
          </DialogHeader>

          {formOpen ? (
            <div className="space-y-4">
              <div>
                <label
                  htmlFor="scheduled-task-prompt"
                  className="text-sm font-medium"
                >
                  {t('chat.promptScheduling.form.prompt')}
                </label>
                <Textarea
                  id="scheduled-task-prompt"
                  value={form.prompt}
                  onChange={(event) =>
                    setFormField('prompt', event.target.value)
                  }
                  className="mt-1"
                  rows={4}
                />
                {config.promptGallery && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="mt-2"
                    onClick={() => {
                      setGalleryOpen(true);
                      fetchPrompts().catch(() => null);
                    }}
                  >
                    {t('chat.promptScheduling.form.chooseGallery')}
                  </Button>
                )}
              </div>

              {modelOptions.length > 1 && (
                <div>
                  <label
                    htmlFor="scheduled-task-model"
                    className="text-sm font-medium"
                  >
                    {t('chat.promptScheduling.form.profile')}
                  </label>
                  <select
                    id="scheduled-task-model"
                    className="mt-1 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                    value={form.model}
                    onChange={(event) =>
                      setFormField('model', event.target.value)
                    }
                  >
                    {modelOptions.map((model: string) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {showProfileSelector && (
                <div>
                  <label
                    htmlFor="scheduled-task-profile"
                    className="text-sm font-medium"
                  >
                    {t('chat.promptScheduling.form.chatProfile')}
                  </label>
                  <select
                    id="scheduled-task-profile"
                    className="mt-1 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                    value={form.chatProfile}
                    onChange={(event) =>
                      setFormField('chatProfile', event.target.value)
                    }
                  >
                    <option value="">
                      {t('chat.promptScheduling.form.defaultAssistant')}
                    </option>
                    {profiles.map((profile: ChatProfile) => (
                      <option key={profile.name} value={profile.name}>
                        {profile.display_name || profile.name}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              <div>
                <label
                  htmlFor="scheduled-task-recurrence"
                  className="text-sm font-medium"
                >
                  {t('chat.promptScheduling.form.recurrence')}
                </label>
                <select
                  id="scheduled-task-recurrence"
                  className="mt-1 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                  value={form.recurrence}
                  onChange={(event) => {
                    const value = event.target.value as Preset | 'custom';
                    if (value === 'custom') {
                      setForm((current) => ({
                        ...current,
                        recurrence: value,
                        cron: buildCustomCron(
                          current.customFrequency,
                          current.customInterval,
                          current.customWeekday,
                          current.customDay,
                          current.customTime
                        )
                      }));
                      return;
                    }
                    setForm((current) => ({
                      ...current,
                      recurrence: value,
                      cron: presetCrons[value]
                    }));
                  }}
                >
                  <option value="daily">
                    {t('chat.promptScheduling.presets.daily')}
                  </option>
                  <option value="weekdays">
                    {t('chat.promptScheduling.presets.weekdays')}
                  </option>
                  <option value="weekly">
                    {t('chat.promptScheduling.presets.weekly')}
                  </option>
                  <option value="monthly">
                    {t('chat.promptScheduling.presets.monthly')}
                  </option>
                  <option value="custom">
                    {t('chat.promptScheduling.presets.custom')}
                  </option>
                </select>
              </div>

              {form.recurrence === 'custom' && (
                <div className="space-y-3 rounded-md border p-3">
                  <div>
                    <label
                      htmlFor="scheduled-task-custom-frequency"
                      className="text-sm font-medium"
                    >
                      {t('chat.promptScheduling.form.customFrequency')}
                    </label>
                    <select
                      id="scheduled-task-custom-frequency"
                      className="mt-1 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                      value={form.customFrequency}
                      onChange={(event) =>
                        setCustomField(
                          'customFrequency',
                          event.target.value as CustomFrequency
                        )
                      }
                    >
                      <option value="hours">
                        {t('chat.promptScheduling.custom.everyHours')}
                      </option>
                      <option value="daily">
                        {t('chat.promptScheduling.custom.daily')}
                      </option>
                      <option value="weekly">
                        {t('chat.promptScheduling.custom.weekly')}
                      </option>
                      <option value="monthly">
                        {t('chat.promptScheduling.custom.monthly')}
                      </option>
                    </select>
                  </div>

                  {form.customFrequency === 'hours' && (
                    <div>
                      <label
                        htmlFor="scheduled-task-custom-interval"
                        className="text-sm font-medium"
                      >
                        {t('chat.promptScheduling.form.interval')}
                      </label>
                      <div className="mt-1 flex items-center gap-2">
                        <input
                          id="scheduled-task-custom-interval"
                          className="flex h-10 w-24 rounded-md border border-input bg-background px-3 py-2 text-sm"
                          type="number"
                          min="1"
                          max="23"
                          step="1"
                          value={form.customInterval}
                          onChange={(event) =>
                            setCustomField(
                              'customInterval',
                              Number(event.target.value)
                            )
                          }
                        />
                        <span className="text-sm text-muted-foreground">
                          {t('chat.promptScheduling.form.hours')}
                        </span>
                      </div>
                    </div>
                  )}

                  {form.customFrequency !== 'hours' && (
                    <div>
                      <label
                        htmlFor="scheduled-task-custom-time"
                        className="text-sm font-medium"
                      >
                        {t('chat.promptScheduling.form.time')}
                      </label>
                      <input
                        id="scheduled-task-custom-time"
                        className="mt-1 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm [color-scheme:light] dark:[color-scheme:dark]"
                        type="time"
                        value={form.customTime}
                        onChange={(event) =>
                          setCustomField('customTime', event.target.value)
                        }
                      />
                    </div>
                  )}

                  {form.customFrequency === 'weekly' && (
                    <div>
                      <label
                        htmlFor="scheduled-task-custom-weekday"
                        className="text-sm font-medium"
                      >
                        {t('chat.promptScheduling.form.weekday')}
                      </label>
                      <select
                        id="scheduled-task-custom-weekday"
                        className="mt-1 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                        value={form.customWeekday}
                        onChange={(event) =>
                          setCustomField('customWeekday', event.target.value)
                        }
                      >
                        <option value="1">
                          {t('chat.promptScheduling.weekdays.monday')}
                        </option>
                        <option value="2">
                          {t('chat.promptScheduling.weekdays.tuesday')}
                        </option>
                        <option value="3">
                          {t('chat.promptScheduling.weekdays.wednesday')}
                        </option>
                        <option value="4">
                          {t('chat.promptScheduling.weekdays.thursday')}
                        </option>
                        <option value="5">
                          {t('chat.promptScheduling.weekdays.friday')}
                        </option>
                        <option value="6">
                          {t('chat.promptScheduling.weekdays.saturday')}
                        </option>
                        <option value="0">
                          {t('chat.promptScheduling.weekdays.sunday')}
                        </option>
                      </select>
                    </div>
                  )}

                  {form.customFrequency === 'monthly' && (
                    <div>
                      <label
                        htmlFor="scheduled-task-custom-day"
                        className="text-sm font-medium"
                      >
                        {t('chat.promptScheduling.form.dayOfMonth')}
                      </label>
                      <input
                        id="scheduled-task-custom-day"
                        className="mt-1 flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                        type="number"
                        min="1"
                        max="31"
                        step="1"
                        value={form.customDay}
                        onChange={(event) =>
                          setCustomField(
                            'customDay',
                            Number(event.target.value)
                          )
                        }
                      />
                    </div>
                  )}
                </div>
              )}

              {hooks.length > 0 && (
                <fieldset className="space-y-2">
                  <legend className="text-sm font-medium">
                    {t('chat.promptScheduling.form.afterHooks')}
                  </legend>
                  {hooks.map((hook) => {
                    const id = `scheduled-task-hook-${hook.name}`;
                    return (
                      <div
                        key={hook.name}
                        className="flex items-center justify-between gap-2 text-sm"
                      >
                        <label htmlFor={id}>{hook.name}</label>
                        <Switch
                          id={id}
                          checked={form.afterHooks.includes(hook.name)}
                          onCheckedChange={(checked) =>
                            setFormField(
                              'afterHooks',
                              checked
                                ? [...form.afterHooks, hook.name]
                                : form.afterHooks.filter(
                                    (name) => name !== hook.name
                                  )
                            )
                          }
                        />
                      </div>
                    );
                  })}
                  {form.afterHooks
                    .filter((name) => !hooks.some((hook) => hook.name === name))
                    .map((name) => {
                      const id = `scheduled-task-hook-${name}`;
                      return (
                        <div
                          key={name}
                          className="flex items-center justify-between gap-2 text-sm text-muted-foreground"
                        >
                          <label htmlFor={id}>
                            {name} (
                            {t('chat.promptScheduling.form.unavailable')})
                          </label>
                          <Switch id={id} checked disabled />
                        </div>
                      );
                    })}
                </fieldset>
              )}

              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => setFormOpen(false)}>
                  {t('common.actions.cancel')}
                </Button>
                <Button onClick={save}>{t('common.actions.submit')}</Button>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
              {tasks.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  <Translator path="chat.promptScheduling.empty" />
                </p>
              ) : (
                tasks.map((task) => (
                  <div
                    key={task.id}
                    className="rounded-md border p-3 space-y-2"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="font-medium whitespace-pre-wrap break-words">
                          {task.prompt}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {task.enabled
                            ? t('chat.promptScheduling.status.active')
                            : t('chat.promptScheduling.status.paused')}{' '}
                          · {describeCron(task.cron, t)} · {task.timezone}
                        </p>
                      </div>
                      <div className="flex shrink-0 gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={t('chat.promptScheduling.actions.edit')}
                          onClick={() => beginEdit(task)}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={
                            task.enabled
                              ? t('chat.promptScheduling.actions.pause')
                              : t('chat.promptScheduling.actions.resume')
                          }
                          onClick={() => toggle(task)}
                        >
                          {task.enabled ? (
                            <Pause className="h-4 w-4" />
                          ) : (
                            <RotateCcw className="h-4 w-4" />
                          )}
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={t('chat.promptScheduling.actions.delete')}
                          onClick={() => setDeleteTarget(task)}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {t('chat.promptScheduling.nextRun')}:{' '}
                      {displayDate(task.nextRunAt)} ·{' '}
                      {t('chat.promptScheduling.lastRun')}:{' '}
                      {displayDate(task.lastRunAt)}
                    </p>
                    {task.lastStatus && (
                      <p className="text-xs" role="status">
                        {t('chat.promptScheduling.lastStatus')}:{' '}
                        {statusLabel(task.lastStatus, t)}
                        {task.lastError ? `. ${task.lastError}` : ''}
                      </p>
                    )}
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => run(task)}
                        disabled={!!task.manualRunRequestedAt}
                      >
                        <Play className="h-3.5 w-3.5 mr-1" />
                        {t('chat.promptScheduling.actions.runNow')}
                      </Button>
                      {task.lastThreadId && (
                        <a
                          className="text-sm underline inline-flex items-center gap-1"
                          href={`/thread/${task.lastThreadId}`}
                        >
                          {t('chat.promptScheduling.actions.viewThread')}{' '}
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={galleryOpen} onOpenChange={setGalleryOpen}>
        <DialogContent aria-describedby={undefined}>
          <DialogHeader>
            <DialogTitle>{t('chat.promptScheduling.galleryTitle')}</DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            {prompts.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {t('chat.promptScheduling.galleryEmpty')}
              </p>
            ) : (
              prompts.map((prompt: IPrompt) => (
                <button
                  type="button"
                  key={prompt.id}
                  className="w-full rounded-md border p-3 text-left hover:bg-muted"
                  onClick={() => {
                    setFormField('prompt', prompt.content);
                    setGalleryOpen(false);
                  }}
                >
                  <span className="block font-medium">{prompt.title}</span>
                  <span className="block text-xs text-muted-foreground line-clamp-2">
                    {prompt.content}
                  </span>
                </button>
              ))
            )}
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(nextOpen) => !nextOpen && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('chat.promptScheduling.delete.title')}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('chat.promptScheduling.delete.description')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.actions.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={remove}>
              {t('common.actions.confirm')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export default PromptSchedulingButton;
