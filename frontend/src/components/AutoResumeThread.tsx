import {
  useChainlitState,
  useChatInteract,
  useChatMessages,
  useChatSession,
  useConfig
} from '@chainlit-ng/react-client';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import PendingInteractionDialog from '@/components/PendingInteractionDialog';

import {
  consumeConfirmedNavigation,
  useHasPendingInteraction
} from '@/hooks/usePendingInteraction';

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
  const hasPendingInteraction = useHasPendingInteraction();
  const [pendingThreadId, setPendingThreadId] = useState<string>();
  // Guards against React StrictMode invoking the route effect twice, which
  // would otherwise clear and resume the same thread twice.
  const resumeTargetRef = useRef<string | undefined>(undefined);

  const resumeThread = () => {
    clear();
    setIdToResume(id);
    if (!config?.dataPersistence) {
      navigate('/');
    }
  };

  useEffect(() => {
    if (resumeTargetRef.current && resumeTargetRef.current !== id) {
      resumeTargetRef.current = undefined;
    }
    // Only resume on a route change, not when New Chat clears the thread.
    if (!config?.threadResumable) return;
    if (id === threadId) {
      resumeTargetRef.current = undefined;
      setPendingThreadId(undefined);
      return;
    }
    if (resumeTargetRef.current === id) return;
    const navigationConfirmed = consumeConfirmedNavigation(id);
    if (hasPendingInteraction && !navigationConfirmed) {
      setPendingThreadId(id);
      return;
    }
    resumeTargetRef.current = id;
    resumeThread();
  }, [config?.threadResumable, id]);

  useEffect(() => {
    if (id !== idToResume) {
      return;
    }
    if (session?.error) {
      resumeTargetRef.current = undefined;
      toast.error("Couldn't resume chat");
      navigate('/');
    }
  }, [session, idToResume, id]);

  useEffect(() => {
    if (resumeThreadError) {
      resumeTargetRef.current = undefined;
      toast.error("Couldn't resume chat: " + resumeThreadError);
      navigate('/');
      setResumeThreadError(undefined);
    }
  }, [resumeThreadError]);

  const handleCancel = () => {
    resumeTargetRef.current = undefined;
    setPendingThreadId(undefined);
    navigate(threadId ? `/thread/${threadId}` : '/');
  };

  return (
    <PendingInteractionDialog
      open={!!pendingThreadId}
      onOpenChange={(open) => {
        if (!open) handleCancel();
      }}
      onConfirm={() => {
        resumeTargetRef.current = id;
        setPendingThreadId(undefined);
        resumeThread();
      }}
      contentId="pending-resume-dialog"
      confirmId="confirm-pending-resume"
    />
  );
}
