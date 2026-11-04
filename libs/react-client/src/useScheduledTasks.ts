import { useCallback, useContext } from 'react';

import { ChainlitContext } from './context';
import { useChainlitState } from './state';
import {
  IScheduledTask,
  IScheduledTaskPayload,
  IScheduledTaskUpdate
} from './types';

const useScheduledTasks = () => {
  const apiClient = useContext(ChainlitContext);
  const [tasks, setTasks] = useChainlitState('scheduledTasks');

  const fetchScheduledTasks = useCallback(async () => {
    const list = await apiClient.listScheduledTasks();
    setTasks(list);
    return list;
  }, [apiClient, setTasks]);

  const createScheduledTask = useCallback(
    async (payload: IScheduledTaskPayload): Promise<IScheduledTask> => {
      const created = await apiClient.createScheduledTask(payload);
      setTasks((prev) => [created, ...prev]);
      return created;
    },
    [apiClient, setTasks]
  );

  const updateScheduledTask = useCallback(
    async (
      id: string,
      payload: IScheduledTaskUpdate
    ): Promise<IScheduledTask> => {
      const updated = await apiClient.updateScheduledTask(id, payload);
      setTasks((prev) => prev.map((task) => (task.id === id ? updated : task)));
      return updated;
    },
    [apiClient, setTasks]
  );

  const deleteScheduledTask = useCallback(
    async (id: string) => {
      await apiClient.deleteScheduledTask(id);
      setTasks((prev) => prev.filter((task) => task.id !== id));
    },
    [apiClient, setTasks]
  );

  const runScheduledTask = useCallback(
    async (id: string): Promise<IScheduledTask> => {
      const updated = await apiClient.runScheduledTask(id);
      setTasks((prev) => prev.map((task) => (task.id === id ? updated : task)));
      return updated;
    },
    [apiClient, setTasks]
  );

  return {
    tasks,
    fetchScheduledTasks,
    createScheduledTask,
    updateScheduledTask,
    deleteScheduledTask,
    runScheduledTask
  };
};

export { useScheduledTasks };
