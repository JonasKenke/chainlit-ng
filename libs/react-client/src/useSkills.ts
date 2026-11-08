import { useCallback, useContext } from 'react';

import { ChainlitContext } from './context';
import { useChainlitState } from './state';
import { ISkill, ISkillExport, ISkillPayload, ISkillUpdate } from './types';

const useSkills = () => {
  const apiClient = useContext(ChainlitContext);
  const [skills, setSkills] = useChainlitState('skills');

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
    },
    [apiClient, setSkills]
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

  return {
    skills,
    fetchSkills,
    createSkill,
    updateSkill,
    deleteSkill,
    importSkill,
    exportSkill
  };
};

export { useSkills };
