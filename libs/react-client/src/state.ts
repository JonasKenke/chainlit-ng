import { useCallback, useContext } from 'react';
import type { Socket } from 'socket.io-client';
import { v4 as uuidv4 } from 'uuid';
import { useStore } from 'zustand';
import { type StoreApi, createStore } from 'zustand/vanilla';

import { ICommand } from './types/command';
import { IMode } from './types/mode';

import type { ChainlitAPI } from './api';
import { ChainlitContext } from './context';
import {
  MCP_STORAGE_KEY,
  parseStoredMcps,
  serializeStoredMcps
} from './mcpStorage';
import {
  IAction,
  IAsk,
  IAuthConfig,
  ICallFn,
  IChainlitConfig,
  IMcp,
  IMessageElement,
  IPrompt,
  IScheduledTask,
  IStep,
  ITasklistElement,
  IUser,
  ThreadHistory
} from './types';
import { groupByDate } from './utils/group';
import { WavRecorder, WavStreamPlayer } from './wavtools';

export interface ISession {
  socket: Socket;
  error?: boolean;
}

export interface ChainlitState {
  threadIdToResume: string | undefined;
  resumeThreadError: string | undefined;
  unavailableThreadId: string | undefined;
  chatProfile: string | undefined;
  sessionId: string;
  session: ISession | undefined;
  actions: IAction[];
  messages: IStep[];
  commands: ICommand[];
  modes: IMode[];
  tokenCount: number;
  loading: boolean;
  askUser: IAsk | undefined;
  wavRecorder: WavRecorder;
  wavStreamPlayer: WavStreamPlayer;
  audioConnection: 'connecting' | 'on' | 'off';
  isAiSpeaking: boolean;
  callFn: ICallFn | undefined;
  chatSettingsInputs: any[];
  chatSettingsDefaultValue: Record<string, any>;
  chatSettingsValue: Record<string, any>;
  elements: IMessageElement[];
  tasklists: ITasklistElement[];
  firstUserInteraction: string | undefined;
  user: IUser | undefined | null;
  config: IChainlitConfig | undefined;
  auth: IAuthConfig | undefined;
  threadHistory: ThreadHistory | undefined;
  sideView:
    { title: string; elements: IMessageElement[]; key?: string } | undefined;
  currentThreadId: string | undefined;
  mcps: IMcp[];
  favoriteMessages: IStep[];
  prompts: IPrompt[];
  scheduledTasks: IScheduledTask[];
}

export type ChainlitStateKey = keyof ChainlitState;
type StateUpdate<T> = T | ((previous: T) => T);
type ChainlitStore = StoreApi<ChainlitState>;

const stores = new WeakMap<ChainlitAPI, ChainlitStore>();
const initialStates = new WeakMap<ChainlitStore, ChainlitState>();
function readStoredMcps(): IMcp[] {
  const raw = localStorage.getItem(MCP_STORAGE_KEY);
  const mcps = parseStoredMcps(raw);
  if (raw === null) return mcps;

  // Rewrite a pre-upgrade payload immediately so stale headers or URL
  // credentials do not sit at rest until the next mcps write or logout.
  const sanitized = serializeStoredMcps(mcps);
  if (sanitized === raw) return mcps;

  if (mcps.length === 0) {
    localStorage.removeItem(MCP_STORAGE_KEY);
  } else {
    localStorage.setItem(MCP_STORAGE_KEY, sanitized);
  }
  return mcps;
}

function initialState(): ChainlitState {
  return {
    threadIdToResume: undefined,
    resumeThreadError: undefined,
    unavailableThreadId: undefined,
    chatProfile: undefined,
    sessionId: uuidv4(),
    session: undefined,
    actions: [],
    messages: [],
    commands: [],
    modes: [],
    tokenCount: 0,
    loading: false,
    askUser: undefined,
    wavRecorder: new WavRecorder(),
    wavStreamPlayer: new WavStreamPlayer(),
    audioConnection: 'off',
    isAiSpeaking: false,
    callFn: undefined,
    chatSettingsInputs: [],
    chatSettingsDefaultValue: {},
    chatSettingsValue: {},
    elements: [],
    tasklists: [],
    firstUserInteraction: undefined,
    user: undefined,
    config: undefined,
    auth: undefined,
    threadHistory: {
      threads: undefined,
      currentThreadId: undefined,
      timeGroupedThreads: undefined,
      pageInfo: undefined
    },
    sideView: undefined,
    currentThreadId: undefined,
    mcps: readStoredMcps(),
    favoriteMessages: [],
    prompts: [],
    scheduledTasks: []
  };
}

export function getChainlitStore(client: ChainlitAPI): ChainlitStore {
  let store = stores.get(client);
  if (!store) {
    const state = initialState();
    store = createStore<ChainlitState>()(() => state);
    stores.set(client, store);
    initialStates.set(store, state);
  }
  return store;
}

export function useChainlitStore<T>(selector: (state: ChainlitState) => T): T {
  const client = useContext(ChainlitContext);
  return useStore(getChainlitStore(client), selector);
}

function writeValue<K extends ChainlitStateKey>(
  store: ChainlitStore,
  key: K,
  update: StateUpdate<ChainlitState[K]>
) {
  const previous = store.getState()[key];
  const value =
    typeof update === 'function'
      ? (update as (previous: ChainlitState[K]) => ChainlitState[K])(previous)
      : update;
  const nextState: Partial<ChainlitState> = { [key]: value };

  if (key === 'chatSettingsInputs') {
    nextState.chatSettingsDefaultValue = getChatSettingsDefaultValue(
      value as any[]
    );
  } else if (key === 'threadHistory' && value) {
    nextState.threadHistory = withGroupedThreads(value as ThreadHistory);
  } else if (key === 'mcps') {
    localStorage.setItem(MCP_STORAGE_KEY, serializeStoredMcps(value as IMcp[]));
  }

  store.setState(nextState);
}

export function useChainlitValue<K extends ChainlitStateKey>(key: K) {
  return useChainlitStore((state) => state[key]);
}

export function useSetChainlitState<K extends ChainlitStateKey>(key: K) {
  const client = useContext(ChainlitContext);
  const store = getChainlitStore(client);
  return useCallback(
    (update: StateUpdate<ChainlitState[K]>) => {
      writeValue(store, key, update);
    },
    [key, store]
  );
}

export function useChainlitState<K extends ChainlitStateKey>(key: K) {
  const value = useChainlitValue(key);
  const setValue = useSetChainlitState(key);
  return [value, setValue] as const;
}

export function useResetChainlitState<K extends ChainlitStateKey>(key: K) {
  const client = useContext(ChainlitContext);
  const store = getChainlitStore(client);

  return useCallback(() => {
    let value = initialStates.get(store)?.[key];
    if (key === 'sessionId') value = uuidv4() as ChainlitState[K];
    if (key === 'chatSettingsValue') {
      value = store.getState().chatSettingsDefaultValue as ChainlitState[K];
    }
    if (key === 'mcps') {
      localStorage.removeItem(MCP_STORAGE_KEY);
      store.setState({ mcps: [] });
      return;
    }
    writeValue(store, key, value as ChainlitState[K]);
  }, [key, store]);
}

export function getChatSettingsDefaultValue(
  inputs: any[]
): Record<string, any> {
  const values: Record<string, any> = {};
  const collect = (items: any[]) => {
    items.forEach((input) => {
      if (Array.isArray(input?.inputs) && input.inputs.length) {
        collect(input.inputs);
      } else if (input?.id !== undefined) {
        values[input.id] = input.initial;
      }
    });
  };
  if (Array.isArray(inputs)) collect(inputs);
  return values;
}

function withGroupedThreads(history: ThreadHistory): ThreadHistory {
  return {
    ...history,
    timeGroupedThreads: history.threads
      ? groupByDate(history.threads)
      : undefined
  };
}
