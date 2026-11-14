import {
  useChainlitState,
  useChatInteract,
  useChatMessages,
  useChatSession,
  useConfig
} from '@chainlit-ng/react-client';
import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

interface Props {
  id: string;
}

export default function AutoResumeThread({ id }: Props) {
  const navigate = useNavigate();
  const { config } = useConfig();
  const { clear, setIdToResume } = useChatInteract();
  const { session, idToResume } = useChatSession();
  const { threadId } = useChatMessages();
  const [resumeThreadError, setResumeThreadError] =
    useChainlitState('resumeThreadError');

  useEffect(() => {
    // Only resume on a route change, not when New Chat clears the thread.
    if (!config?.threadResumable || id === threadId) return;
    clear();
    setIdToResume(id);
    if (!config?.dataPersistence) {
      navigate('/');
    }
  }, [config?.threadResumable, id]);

  useEffect(() => {
    if (id !== idToResume) {
      return;
    }
    if (session?.error) {
      toast.error("Couldn't resume chat");
      navigate('/');
    }
  }, [session, idToResume, id]);

  useEffect(() => {
    if (resumeThreadError) {
      toast.error("Couldn't resume chat: " + resumeThreadError);
      navigate('/');
      setResumeThreadError(undefined);
    }
  }, [resumeThreadError]);

  return null;
}
