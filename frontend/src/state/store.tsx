import {
  type PropsWithChildren,
  createContext,
  useContext,
  useState
} from 'react';
import { useStore } from 'zustand';
import { type StoreApi, createStore } from 'zustand/vanilla';

import type { ICommand } from '@chainlit/react-client';

type Update<T> = T | ((current: T) => T);

export interface IAttachment {
  id: string;
  serverId?: string;
  name: string;
  size: number;
  type: string;
  uploadProgress?: number;
  uploaded?: boolean;
  cancel?: () => void;
  remove?: () => void;
  file?: File;
}

interface AppStateData {
  attachments: IAttachment[];
  persistentCommand: ICommand | undefined;
  chatSettingsOpen: boolean;
  chatSettingsSidebarOpen: boolean;
  userEnv: Record<string, string>;
}

export interface AppState extends AppStateData {
  setAttachments: (value: Update<IAttachment[]>) => void;
  setPersistentCommand: (value: Update<ICommand | undefined>) => void;
  setChatSettingsOpen: (value: Update<boolean>) => void;
  setChatSettingsSidebarOpen: (value: Update<boolean>) => void;
  setUserEnv: (value: Update<Record<string, string>>) => void;
}

const AppStateContext = createContext<StoreApi<AppState> | null>(null);

function loadUserEnv(): Record<string, string> {
  try {
    const stored = localStorage.getItem('userEnv');
    return stored ? JSON.parse(stored) : {};
  } catch {
    return {};
  }
}

export function createAppStateStore(initial: Partial<AppStateData> = {}) {
  return createStore<AppState>()((set) => ({
    attachments: initial.attachments ?? [],
    persistentCommand: initial.persistentCommand,
    chatSettingsOpen: initial.chatSettingsOpen ?? false,
    chatSettingsSidebarOpen: initial.chatSettingsSidebarOpen ?? false,
    userEnv: initial.userEnv ?? loadUserEnv(),
    setAttachments: (value) =>
      set((state) => ({
        attachments:
          typeof value === 'function' ? value(state.attachments) : value
      })),
    setPersistentCommand: (value) =>
      set((state) => ({
        persistentCommand:
          typeof value === 'function' ? value(state.persistentCommand) : value
      })),
    setChatSettingsOpen: (value) =>
      set((state) => ({
        chatSettingsOpen:
          typeof value === 'function' ? value(state.chatSettingsOpen) : value
      })),
    setChatSettingsSidebarOpen: (value) =>
      set((state) => ({
        chatSettingsSidebarOpen:
          typeof value === 'function'
            ? value(state.chatSettingsSidebarOpen)
            : value
      })),
    setUserEnv: (value) =>
      set((state) => {
        const userEnv =
          typeof value === 'function' ? value(state.userEnv) : value;
        localStorage.setItem('userEnv', JSON.stringify(userEnv));
        return { userEnv };
      })
  }));
}

export function AppStateProvider({
  children,
  store: providedStore
}: PropsWithChildren<{ store?: StoreApi<AppState> }>) {
  const [store] = useState(() => providedStore ?? createAppStateStore());
  return (
    <AppStateContext.Provider value={store}>
      {children}
    </AppStateContext.Provider>
  );
}

export function useAppStore<T>(selector: (state: AppState) => T): T {
  const store = useContext(AppStateContext);
  if (!store) throw new Error('Missing AppStateProvider');
  return useStore(store, selector);
}
