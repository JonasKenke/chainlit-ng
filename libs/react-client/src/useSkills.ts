import { useCallback, useContext } from 'react';

import { ChainlitContext } from './context';
import { useChainlitState } from './state';
import {
  ISharedSkill,
  ISkill,
  ISkillExport,
  ISkillPayload,
  ISkillSeenAck,
  ISkillShareResult,
  ISkillUpdate,
  ISkillUpdateNotice,
  ISkillVersion,
  ISkillVersionSummary,
  SkillUpdatePayload,
  SkillVersionQuery
} from './types';
import { mergeSkillUpdates } from './utils/skill';

const useSkills = () => {
  const apiClient = useContext(ChainlitContext);
  const [skills, setSkills] = useChainlitState('skills');
  const [skillUpdates, setSkillUpdates] = useChainlitState('skillUpdates');

  const fetchSkills = useCallback(async () => {
    const list = await apiClient.listSkills();
    setSkills(list);
    return list;
  }, [apiClient, setSkills]);

  const createSkill = useCallback(
    async (payload: ISkillPayload): Promise<ISkill> => {
      const created = await apiClient.createSkill(payload);
      setSkills((previous) => [created, ...previous]);
      return created;
    },
    [apiClient, setSkills]
  );

  const updateSkill = useCallback(
    async (id: string, fields: ISkillUpdate): Promise<ISkill> => {
      const updated = await apiClient.updateSkill(id, fields);
      setSkills((previous) =>
        previous.map((skill) => (skill.id === id ? updated : skill))
      );
      return updated;
    },
    [apiClient, setSkills]
  );

  const deleteSkill = useCallback(
    async (id: string): Promise<void> => {
      await apiClient.deleteSkill(id);
      setSkills((previous) => previous.filter((skill) => skill.id !== id));
      setSkillUpdates((previous) =>
        previous.filter((update) => update.skillId !== id)
      );
    },
    [apiClient, setSkills, setSkillUpdates]
  );

  const importSkill = useCallback(
    async (markdown: string, importedFrom: string): Promise<ISkill> => {
      const created = await apiClient.importSkill(markdown, importedFrom);
      setSkills((previous) => [created, ...previous]);
      return created;
    },
    [apiClient, setSkills]
  );

  const exportSkill = useCallback(
    async (id: string): Promise<ISkillExport> => apiClient.exportSkill(id),
    [apiClient]
  );

  const shareSkill = useCallback(
    async (id: string, isShared: boolean): Promise<ISkillShareResult> => {
      const result = await apiClient.shareSkill(id, isShared);
      setSkills((previous) =>
        previous.map((skill) => (skill.id === id ? result.skill : skill))
      );
      return result;
    },
    [apiClient, setSkills]
  );

  const getSharedSkill = useCallback(
    async (id: string): Promise<ISharedSkill> => apiClient.getSharedSkill(id),
    [apiClient]
  );

  const addSharedSkill = useCallback(
    async (sharedId: string, versionId?: string): Promise<ISkill> => {
      const created = await apiClient.addSharedSkill(sharedId, versionId);
      setSkills((previous) => [created, ...previous]);
      return created;
    },
    [apiClient, setSkills]
  );

  const fetchSkillVersions = useCallback(
    async (
      id: string,
      options?: SkillVersionQuery
    ): Promise<ISkillVersionSummary[]> =>
      apiClient.fetchSkillVersions(id, options),
    [apiClient]
  );

  const fetchSkillVersion = useCallback(
    async (
      id: string,
      versionId: string,
      options?: SkillVersionQuery
    ): Promise<ISkillVersion> =>
      apiClient.fetchSkillVersion(id, versionId, options),
    [apiClient]
  );

  /** Refresh notices; acknowledged ones survive so their row marker persists. */
  const fetchSkillUpdates = useCallback(async (): Promise<
    ISkillUpdateNotice[]
  > => {
    const list = await apiClient.fetchSkillUpdates();
    let merged: ISkillUpdateNotice[] = [];
    setSkillUpdates((previous) => {
      merged = mergeSkillUpdates(previous, list);
      return merged;
    });
    return merged;
  }, [apiClient, setSkillUpdates]);

  const ackSkillUpdate = useCallback(
    async (skillId: string, version: number): Promise<ISkillSeenAck> => {
      const ack = await apiClient.ackSkillUpdate(skillId, version);
      setSkillUpdates((previous) =>
        previous.map((update) =>
          update.skillId === skillId
            ? {
                ...update,
                seenVersion: Math.max(update.seenVersion ?? 0, ack.version),
                seenVersionId: ack.updateSeenVersionId ?? update.seenVersionId
              }
            : update
        )
      );
      return ack;
    },
    [apiClient, setSkillUpdates]
  );

  /** Apply a source version; the server stores the copy disabled. */
  const updateSkillFromSource = useCallback(
    async (id: string, payload: SkillUpdatePayload): Promise<ISkill> => {
      const updated = await apiClient.updateSkillFromSource(id, payload);
      setSkills((previous) =>
        previous.map((skill) => (skill.id === id ? updated : skill))
      );
      setSkillUpdates((previous) =>
        previous.filter((update) => update.skillId !== id)
      );
      return updated;
    },
    [apiClient, setSkills, setSkillUpdates]
  );

  const detachSkill = useCallback(
    async (id: string): Promise<ISkill> => {
      const updated = await apiClient.detachSkill(id);
      setSkills((previous) =>
        previous.map((skill) => (skill.id === id ? updated : skill))
      );
      setSkillUpdates((previous) =>
        previous.filter((update) => update.skillId !== id)
      );
      return updated;
    },
    [apiClient, setSkills, setSkillUpdates]
  );

  return {
    skills,
    skillUpdates,
    fetchSkills,
    createSkill,
    updateSkill,
    deleteSkill,
    importSkill,
    exportSkill,
    shareSkill,
    getSharedSkill,
    addSharedSkill,
    fetchSkillVersions,
    fetchSkillVersion,
    fetchSkillUpdates,
    ackSkillUpdate,
    updateSkillFromSource,
    detachSkill
  };
};

export { useSkills };
