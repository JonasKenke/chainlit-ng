export interface IScheduledTask {
  id: string;
  userId: string;
  userIdentifier: string;
  prompt: string;
  chatProfile: string | null;
  model?: string | null;
  cron: string;
  timezone: string;
  enabled: boolean;
  afterHooks: string[];
  nextRunAt: string;
  manualRunRequestedAt: string | null;
  lastRunAt: string | null;
  lastStatus: string | null;
  lastError: string | null;
  lastThreadId: string | null;
  leaseId: string | null;
  leaseUntil: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface IScheduledTaskHook {
  name: string;
  default: boolean;
}

export interface IScheduledTaskPayload {
  prompt: string;
  chatProfile?: string | null;
  model?: string | null;
  cron: string;
  timezone: string;
  enabled?: boolean;
  afterHooks?: string[];
}

export interface IScheduledTaskUpdate {
  prompt?: string;
  chatProfile?: string | null;
  model?: string | null;
  cron?: string;
  timezone?: string;
  enabled?: boolean;
  afterHooks?: string[];
}
