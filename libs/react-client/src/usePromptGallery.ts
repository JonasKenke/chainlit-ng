import { useCallback, useContext } from 'react';

import {
  IPrompt,
  IPromptDiff,
  IPromptUpdate,
  IPromptVersion,
  IPromptVersionSummary,
  PromptSyncPayload,
  PromptVersionQuery
} from './types/prompt';

import { ChainlitContext } from './context';
import { useChainlitState } from './state';
import { mergePromptUpdates } from './utils/prompt';

const usePromptGallery = () => {
  const apiClient = useContext(ChainlitContext);
  const [prompts, setPrompts] = useChainlitState('prompts');
  const [promptUpdates, setPromptUpdates] = useChainlitState('promptUpdates');

  const fetchPrompts = useCallback(async () => {
    const list = await apiClient.listPrompts();
    setPrompts(list);
  }, [apiClient, setPrompts]);

  const savePrompt = useCallback(
    async (title: string, content: string): Promise<IPrompt> => {
      const created = await apiClient.createPrompt(title, content);
      setPrompts((prev) => [created, ...prev]);
      return created;
    },
    [apiClient, setPrompts]
  );

  const editPrompt = useCallback(
    async (
      id: string,
      fields: { title?: string; content?: string }
    ): Promise<IPrompt> => {
      const updated = await apiClient.updatePrompt(id, fields);
      setPrompts((prev) => prev.map((p) => (p.id === id ? updated : p)));
      return updated;
    },
    [apiClient, setPrompts]
  );

  const removePrompt = useCallback(
    async (id: string): Promise<void> => {
      await apiClient.deletePrompt(id);
      setPrompts((prev) => prev.filter((p) => p.id !== id));
      setPromptUpdates((prev) => prev.filter((u) => u.promptId !== id));
    },
    [apiClient, setPrompts, setPromptUpdates]
  );

  const sharePrompt = useCallback(
    async (id: string, isShared: boolean): Promise<string> => {
      const { prompt, shareUrl } = await apiClient.sharePrompt(id, isShared);
      setPrompts((prev) => prev.map((p) => (p.id === id ? prompt : p)));
      return shareUrl;
    },
    [apiClient, setPrompts]
  );

  const addSharedPrompt = useCallback(
    async (sharedId: string, versionId?: string): Promise<IPrompt> => {
      const created = await apiClient.addSharedPrompt(sharedId, versionId);
      setPrompts((prev) => [created, ...prev]);
      return created;
    },
    [apiClient, setPrompts]
  );

  /**
   * Refresh the update notices. Entries already seen this session survive a
   * server response that no longer lists them (they were acknowledged), so the
   * quiet per-row marker persists until the copy is synced, detached or
   * removed. A newer notice always wins.
   */
  const fetchPromptUpdates = useCallback(async (): Promise<IPromptUpdate[]> => {
    const list = await apiClient.fetchPromptUpdates();
    let merged: IPromptUpdate[] = [];
    setPromptUpdates((prev) => {
      merged = mergePromptUpdates(prev, list);
      return merged;
    });
    return merged;
  }, [apiClient, setPromptUpdates]);

  const ackPromptUpdate = useCallback(
    async (promptId: string, version: number): Promise<number> => {
      const ack = await apiClient.ackPromptUpdates(promptId, version);
      setPromptUpdates((prev) =>
        prev.map((update) =>
          update.promptId === promptId
            ? {
                ...update,
                notifiedVersion: Math.max(
                  update.notifiedVersion,
                  ack.notifiedVersion
                )
              }
            : update
        )
      );
      return ack.notifiedVersion;
    },
    [apiClient, setPromptUpdates]
  );

  const fetchPromptVersions = useCallback(
    async (
      id: string,
      options?: PromptVersionQuery
    ): Promise<IPromptVersionSummary[]> =>
      apiClient.fetchPromptVersions(id, options),
    [apiClient]
  );

  const fetchPromptVersion = useCallback(
    async (
      id: string,
      versionId: string,
      options?: PromptVersionQuery
    ): Promise<IPromptVersion> =>
      apiClient.fetchPromptVersion(id, versionId, options),
    [apiClient]
  );

  const fetchPromptDiff = useCallback(
    async (
      id: string,
      to: string,
      from: 'pinned' | 'local' = 'pinned'
    ): Promise<IPromptDiff> => apiClient.fetchPromptDiff(id, to, from),
    [apiClient]
  );

  const syncPrompt = useCallback(
    async (id: string, payload: PromptSyncPayload): Promise<IPrompt> => {
      const updated = await apiClient.syncPrompt(id, payload);
      setPrompts((prev) => prev.map((p) => (p.id === id ? updated : p)));
      // The local head now matches the update that was offered.
      setPromptUpdates((prev) => prev.filter((u) => u.promptId !== id));
      return updated;
    },
    [apiClient, setPrompts, setPromptUpdates]
  );

  const detachPrompt = useCallback(
    async (id: string): Promise<IPrompt> => {
      const updated = await apiClient.detachPrompt(id);
      setPrompts((prev) => prev.map((p) => (p.id === id ? updated : p)));
      setPromptUpdates((prev) => prev.filter((u) => u.promptId !== id));
      return updated;
    },
    [apiClient, setPrompts, setPromptUpdates]
  );

  return {
    prompts,
    promptUpdates,
    fetchPrompts,
    savePrompt,
    editPrompt,
    removePrompt,
    sharePrompt,
    addSharedPrompt,
    fetchPromptVersions,
    fetchPromptVersion,
    fetchPromptUpdates,
    ackPromptUpdate,
    fetchPromptDiff,
    syncPrompt,
    detachPrompt
  };
};

export { usePromptGallery };
