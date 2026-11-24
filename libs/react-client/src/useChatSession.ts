import { debounce } from 'lodash';
import { useCallback, useContext, useEffect, useMemo, useRef } from 'react';
import io from 'socket.io-client';
import { toast } from 'sonner';
import {
  getChainlitStore,
  useChainlitState,
  useChainlitStore,
  useResetChainlitState,
  useSetChainlitState
} from 'src/state';
import {
  IAction,
  IApproval,
  ICommand,
  IElement,
  IMcp,
  IMessageElement,
  IMode,
  IQuestion,
  IStep,
  ITasklistElement,
  IThread
} from 'src/types';
import {
  applyMcpConnectResponse,
  markMcpAuthorizationRequired,
  markMcpConnected,
  markMcpFailed
} from 'src/utils/mcp';
import {
  addMessage,
  deleteMessageById,
  updateMessageById,
  updateMessageContentById
} from 'src/utils/message';

import { OutputAudioChunk } from './types/audio';

import { ClientError } from './api';
import { ChainlitContext } from './context';
import type { IToken } from './useChatData';

function approvalFromStep(step: IStep): IApproval | null {
  const raw = (step.metadata as Record<string, any> | undefined)?.approval;
  let record = raw;
  if (typeof raw === 'string') {
    try {
      record = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!record || typeof record !== 'object' || !record.id) return null;
  // Questions share the approval metadata slot; they must not hydrate as cards.
  if (record.kind === 'question') return null;
  // Older servers and their persisted records only have `title`.
  const subtitle = record.subtitle ?? record.title ?? null;
  return {
    id: record.id,
    stepId: record.stepId ?? step.id,
    threadId: record.threadId ?? '',
    tool: record.tool ?? step.name,
    subtitle,
    title: record.title ?? subtitle,
    content: record.content ?? null,
    status: record.status ?? 'orphaned',
    reason: record.reason ?? null,
    live: false,
    requestedBy: record.requestedBy ?? null,
    requestedAt: record.requestedAt ?? null,
    expiresAt: record.expiresAt ?? null,
    // Arguments are not persisted; the read-only card shows the redacted
    // preview. The live event is the only source of full arguments.
    arguments: record.editedArgumentsPreview ?? record.argumentsPreview ?? {},
    argumentsPreview: record.argumentsPreview ?? null,
    editedArgumentsPreview: record.editedArgumentsPreview ?? null,
    editAllowed: Boolean(record.editAllowed),
    decidedBy: record.decidedBy ?? null,
    decidedAt: record.decidedAt ?? null,
    decision: record.decision ?? null,
    terminalReason: record.terminalReason ?? null,
    revision: record.revision
  };
}

function questionFromStep(step: IStep): IQuestion | null {
  const raw = (step.metadata as Record<string, any> | undefined)?.approval;
  let record = raw;
  if (typeof raw === 'string') {
    try {
      record = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (
    !record ||
    typeof record !== 'object' ||
    record.kind !== 'question' ||
    !record.id
  ) {
    return null;
  }
  return {
    kind: 'question',
    id: record.id,
    stepId: record.stepId ?? step.id,
    threadId: record.threadId ?? '',
    status: record.status ?? 'orphaned',
    prompt: record.prompt ?? '',
    options: Array.isArray(record.options) ? record.options : [],
    allowFreeText: Boolean(record.allowFreeText),
    questions: Array.isArray(record.questions) ? record.questions : [],
    structured: Boolean(record.structured),
    multi: Boolean(record.multi),
    requestedAt: record.requestedAt ?? null,
    expiresAt: record.expiresAt ?? null,
    revision: record.revision,
    decidedBy: record.decidedBy ?? null,
    decidedAt: record.decidedAt ?? null,
    answer: record.answer ?? null,
    terminalReason: record.terminalReason ?? null,
    terminalAt: record.terminalAt ?? null,
    live: false
  };
}

const useChatSession = () => {
  const client = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const config = useChainlitStore((state) => state.config);

  const [session, setSession] = useChainlitState('session');
  const setIsAiSpeaking = useSetChainlitState('isAiSpeaking');
  const setAudioConnection = useSetChainlitState('audioConnection');
  const resetChatSettingsValue = useResetChainlitState('chatSettingsValue');
  const setChatSettingsValue = useSetChainlitState('chatSettingsValue');
  const setFirstUserInteraction = useSetChainlitState('firstUserInteraction');
  const setLoading = useSetChainlitState('loading');
  const setMcps = useSetChainlitState('mcps');
  const wavStreamPlayer = useChainlitStore((state) => state.wavStreamPlayer);
  const wavRecorder = useChainlitStore((state) => state.wavRecorder);
  const setMessages = useSetChainlitState('messages');
  const setAskUser = useSetChainlitState('askUser');
  const setCallFn = useSetChainlitState('callFn');
  const setCommands = useSetChainlitState('commands');
  const setModes = useSetChainlitState('modes');
  const setSideView = useSetChainlitState('sideView');
  const setElements = useSetChainlitState('elements');
  const setTasklists = useSetChainlitState('tasklists');
  const setActions = useSetChainlitState('actions');
  const setApprovals = useSetChainlitState('approvals');
  const setQuestions = useSetChainlitState('questions');
  const setChatSettingsInputs = useSetChainlitState('chatSettingsInputs');
  const setTokenCount = useSetChainlitState('tokenCount');
  const [chatProfile, setChatProfile] = useChainlitState('chatProfile');
  const idToResume = useChainlitStore((state) => state.threadIdToResume);
  const setThreadResumeError = useSetChainlitState('resumeThreadError');
  const setUnavailableThreadId = useSetChainlitState('unavailableThreadId');
  const setFavoriteMessages = useSetChainlitState('favoriteMessages');
  const setThreadHistory = useSetChainlitState('threadHistory');

  const [currentThreadId, setCurrentThreadId] =
    useChainlitState('currentThreadId');

  // Use currentThreadId as thread id in websocket header
  useEffect(() => {
    if (session?.socket) {
      session.socket.auth['threadId'] = currentThreadId || '';
    }
  }, [currentThreadId]);

  // Only the latest connect may open its socket. Closing an older socket is
  // not enough: it could still resolve after a newer connect and replace it.
  const connectIdRef = useRef(0);

  const _connect = useCallback(
    async ({
      transports,
      userEnv
    }: {
      transports?: string[];
      userEnv: Record<string, string>;
    }) => {
      const connectId = ++connectIdRef.current;
      const { protocol, host, pathname } = new URL(client.httpEndpoint);
      const uri = `${protocol}//${host}`;
      const path =
        pathname && pathname !== '/'
          ? `${pathname}/ws/socket.io`
          : '/ws/socket.io';

      try {
        await client.stickyCookie(sessionId);
      } catch (err) {
        console.error(`Failed to set sticky session cookie: ${err}`);
      }

      // A newer connect was requested while the cookie was in flight.
      if (connectId !== connectIdRef.current) return;

      const socket = io(uri, {
        path,
        withCredentials: true,
        transports,
        auth: {
          clientType: client.type,
          sessionId,
          threadId: idToResume || '',
          userEnv: JSON.stringify(userEnv),
          chatProfile: chatProfile ? encodeURIComponent(chatProfile) : ''
        }
      });
      setSession((old) => {
        old?.socket?.removeAllListeners();
        old?.socket?.close();
        return {
          socket
        };
      });

      socket.on('connect', () => {
        socket.emit('connection_successful');
        setSession((s) => ({ ...s!, error: false }));
        socket.emit('fetch_favorites');
        const configuredServers = config?.features.mcp?.servers ?? [];
        setMcps((prev) => {
          const seen = new Set<string>();
          const mcps: IMcp[] = [];
          for (const mcp of prev) {
            if (seen.has(mcp.name)) continue;
            seen.add(mcp.name);
            const configured = configuredServers.find(
              (server) => server.name === mcp.name
            );
            mcps.push(
              configured
                ? {
                    name: mcp.name,
                    tools: mcp.tools,
                    status: mcp.status,
                    type: configured.type
                  }
                : mcp
            );
          }

          mcps.forEach((mcp) => {
            const promise =
              mcp.isUserProvided && mcp.clientType && mcp.url
                ? client.connectUserMcp(
                    sessionId,
                    mcp.name,
                    mcp.clientType,
                    mcp.url,
                    mcp.headers,
                    mcp.useOAuth
                  )
                : client.connectMcp(sessionId, mcp.name);
            promise
              .then((response) => {
                setMcps((prev) =>
                  applyMcpConnectResponse(prev, mcp.name, response)
                );
              })
              .catch((error: ClientError) => {
                setMcps((prev) => markMcpFailed(prev, mcp.name, error?.detail));
              });
          });

          return mcps.map((mcp) => ({ ...mcp, status: 'connecting' }));
        });
      });

      socket.on('connect_error', (_) => {
        setSession((s) => ({ ...s!, error: true }));
      });

      socket.on(
        'mcp_authorization_required',
        ({ name, url }: { name: string; url: string }) => {
          setMcps((prev) => markMcpAuthorizationRequired(prev, name, url));
        }
      );

      socket.on('mcp_connected', ({ mcp }: { mcp: IMcp }) => {
        setMcps((prev) => markMcpConnected(prev, mcp));
      });

      socket.on(
        'mcp_connection_failed',
        ({ name, detail }: { name: string; detail: string }) => {
          setMcps((prev) => markMcpFailed(prev, name, detail));
        }
      );

      socket.on('task_start', () => {
        setLoading(true);
      });

      socket.on('task_end', () => {
        setLoading(false);
      });

      socket.on('reload', () => {
        socket.emit('clear_session');
        window.location.reload();
      });

      socket.on('audio_connection', async (state: 'on' | 'off') => {
        if (state === 'on') {
          let isFirstChunk = true;
          const startTime = Date.now();
          const mimeType = 'pcm16';
          try {
            await wavRecorder.begin();
            await wavStreamPlayer.connect();
            await wavRecorder.record(async (data) => {
              const elapsedTime = Date.now() - startTime;
              socket.emit('audio_chunk', {
                isStart: isFirstChunk,
                mimeType,
                elapsedTime,
                data: data.mono
              });
              isFirstChunk = false;
            });
            wavStreamPlayer.onStop = () => setIsAiSpeaking(false);
          } catch {
            try {
              await wavRecorder.end();
            } catch {
              // ignored
            }
            await wavStreamPlayer.interrupt();
            socket.emit('audio_end');
            setAudioConnection('off');
            return;
          }
        } else {
          await wavRecorder.end();
          await wavStreamPlayer.interrupt();
        }
        setAudioConnection(state);
      });

      socket.on('audio_chunk', (chunk: OutputAudioChunk) => {
        wavStreamPlayer.add16BitPCM(chunk.data, chunk.track);
        setIsAiSpeaking(true);
      });

      socket.on('audio_interrupt', () => {
        wavStreamPlayer.interrupt();
      });

      socket.on('resume_thread', (thread: IThread) => {
        const isReadOnlyView = Boolean(
          (thread as any)?.metadata?.viewer_read_only
        );
        if (!isReadOnlyView && idToResume && thread.id !== idToResume) {
          window.location.href = `/thread/${thread.id}`;
        }
        if (!isReadOnlyView && idToResume) {
          setCurrentThreadId(thread.id);
        }
        let messages: IStep[] = [];
        for (const step of thread.steps) {
          messages = addMessage(messages, step);
        }
        if (thread.metadata?.chat_profile) {
          setChatProfile(thread.metadata?.chat_profile);
        }
        if (thread.metadata?.chat_settings) {
          setChatSettingsValue(thread.metadata?.chat_settings);
        }
        setMessages(messages);
        const restored = (thread.steps || [])
          .map(approvalFromStep)
          .filter((approval): approval is IApproval => approval !== null);
        setApprovals((old) => {
          const next = old.filter(
            (approval) => approval.threadId === thread.id
          );
          for (const item of restored) {
            const index = next.findIndex((approval) => approval.id === item.id);
            if (index === -1) {
              next.push(item);
              continue;
            }
            const existing = next[index];
            if (existing.status === 'pending' && item.status === 'pending') {
              // The persisted record only has the redacted preview. Keep the
              // live arguments the user is actually approving.
              next[index] = {
                ...item,
                arguments: existing.arguments ?? item.arguments,
                revision: Math.max(existing.revision ?? 0, item.revision ?? 0)
              };
            } else if (
              existing.status !== 'pending' &&
              item.status === 'pending'
            ) {
              // A stale persisted pending row must not reopen a terminal card.
              continue;
            } else {
              next[index] = item;
            }
          }
          return next;
        });
        const restoredQuestions = (thread.steps || [])
          .map(questionFromStep)
          .filter((question): question is IQuestion => question !== null);
        setQuestions((old) => {
          const next = old.filter(
            (question) => question.threadId === thread.id
          );
          for (const item of restoredQuestions) {
            const index = next.findIndex((question) => question.id === item.id);
            if (index === -1) {
              next.push(item);
              continue;
            }
            const existing = next[index];
            if (existing.live) {
              // A same-session reconnect must not downgrade the answerable
              // card or reopen a terminal one from a stale pending row.
              next[index] =
                existing.status === 'pending'
                  ? { ...item, live: true }
                  : { ...existing, live: true };
              continue;
            }
            next[index] = item;
          }
          return next;
        });
        const elements = (thread.elements || []).filter(
          (e): e is IElement => e != null
        );
        // Resolve element URLs from chainlitKey when url is missing,
        // matching the behavior of the 'element' socket event handler.
        elements.forEach((element) => {
          if (!element.url && element.chainlitKey) {
            element.url = client.getElementUrl(element.chainlitKey, sessionId);
          }
        });
        setTasklists(
          (elements as ITasklistElement[]).filter((e) => e.type === 'tasklist')
        );
        setElements(
          (elements as IMessageElement[]).filter(
            (e) => ['avatar', 'tasklist'].indexOf(e.type) === -1
          )
        );
      });

      socket.on('resume_thread_error', (error?: string) => {
        setThreadResumeError(error);
      });

      socket.on(
        'resume_thread_unavailable',
        (event: { thread_id: string; message: string }) => {
          if (event.thread_id !== idToResume) return;
          setUnavailableThreadId(event.thread_id);
          setLoading(false);
          toast.info(event.message);
        }
      );

      socket.on('new_message', (message: IStep) => {
        setMessages((oldMessages) => addMessage(oldMessages, message));
      });

      socket.on(
        'first_interaction',
        (event: { interaction: string; thread_id: string }) => {
          setFirstUserInteraction(event.interaction);
          setCurrentThreadId(event.thread_id);
        }
      );

      socket.on(
        'thread_title_updated',
        ({ thread_id, name }: { thread_id: string; name: string }) => {
          setThreadHistory((history) => {
            if (!history?.threads) return history;

            return {
              ...history,
              threads: history.threads.map((thread) =>
                thread.id === thread_id ? { ...thread, name } : thread
              )
            };
          });
        }
      );

      socket.on('update_message', (message: IStep) => {
        setMessages((oldMessages) =>
          updateMessageById(oldMessages, message.id, message)
        );
      });

      socket.on('delete_message', (message: IStep) => {
        setMessages((oldMessages) =>
          deleteMessageById(oldMessages, message.id)
        );
      });

      socket.on('stream_start', (message: IStep) => {
        setMessages((oldMessages) => addMessage(oldMessages, message));
      });

      socket.on(
        'stream_token',
        ({ id, token, isSequence, isInput }: IToken) => {
          setMessages((oldMessages) =>
            updateMessageContentById(
              oldMessages,
              id,
              token,
              isSequence,
              isInput
            )
          );
        }
      );

      socket.on('ask', ({ msg, spec }, callback) => {
        setAskUser({ spec, callback, parentId: msg.parentId });
        setMessages((oldMessages) => addMessage(oldMessages, msg));

        setLoading(false);
      });

      socket.on('ask_timeout', () => {
        setAskUser(undefined);
        setLoading(false);
      });

      socket.on('clear_ask', () => {
        setAskUser(undefined);
      });

      socket.on('call_fn', ({ name, args }, callback) => {
        setCallFn({ name, args, callback });
      });

      socket.on('clear_call_fn', () => {
        setCallFn(undefined);
      });

      socket.on('call_fn_timeout', () => {
        setCallFn(undefined);
      });

      socket.on('chat_settings', (inputs: any) => {
        setChatSettingsInputs(inputs);
        resetChatSettingsValue();
      });

      socket.on('set_commands', (commands: ICommand[]) => {
        setCommands(commands);
      });

      socket.on('set_modes', (modes: IMode[]) => {
        setModes(modes);
      });

      socket.on('mode_change', (values: Record<string, string>) => {
        setModes((modes) =>
          modes.map((mode) => {
            const selectedId = values[mode.id];
            if (!selectedId) return mode;
            return {
              ...mode,
              options: mode.options.map((option) => ({
                ...option,
                default: option.id === selectedId
              }))
            };
          })
        );
      });

      socket.on('set_favorites', (steps: IStep[]) => {
        setFavoriteMessages(steps);
      });

      socket.on('set_sidebar_title', (title: string) => {
        setSideView((prev) => {
          if (prev?.title === title) return prev;
          return { title, elements: prev?.elements || [] };
        });
      });

      socket.on(
        'set_sidebar_elements',
        ({ elements, key }: { elements: IMessageElement[]; key?: string }) => {
          if (!elements.length) {
            setSideView(undefined);
          } else {
            elements.forEach((element) => {
              if (!element.url && element.chainlitKey) {
                element.url = client.getElementUrl(
                  element.chainlitKey,
                  sessionId
                );
              }
            });
            setSideView((prev) => {
              if (prev?.key === key) return prev;
              return { title: prev?.title || '', elements: elements, key };
            });
          }
        }
      );

      socket.on('open_side_view', ({ id }: { id: string }) => {
        const element = getChainlitStore(client)
          .getState()
          .elements.find((element) => element.id === id);
        if (!element) {
          console.debug(`open_side_view: element ${id} not found`);
          return;
        }
        setSideView({ title: element.name, elements: [element] });
      });

      socket.on('element', (element: IElement) => {
        if (!element.url && element.chainlitKey) {
          element.url = client.getElementUrl(element.chainlitKey, sessionId);
        }

        if (element.type === 'tasklist') {
          setTasklists((old) => {
            const index = old.findIndex((e) => e.id === element.id);
            if (index === -1) {
              return [...old, element];
            } else {
              return [...old.slice(0, index), element, ...old.slice(index + 1)];
            }
          });
        } else {
          setElements((old) => {
            const index = old.findIndex((e) => e.id === element.id);
            if (index === -1) {
              return [...old, element];
            } else {
              return [...old.slice(0, index), element, ...old.slice(index + 1)];
            }
          });
        }
      });

      socket.on('remove_element', (remove: { id: string }) => {
        setElements((old) => {
          return old.filter((e) => e.id !== remove.id);
        });
        setTasklists((old) => {
          return old.filter((e) => e.id !== remove.id);
        });
      });

      socket.on('action', (action: IAction) => {
        setActions((old) => [...old, action]);
      });

      socket.on('approval', (approval: IApproval) => {
        setApprovals((old) => {
          const index = old.findIndex((item) => item.id === approval.id);
          if (index === -1) return [...old, { ...approval, live: true }];
          const previous = old[index];
          // A late pending event must not overwrite a terminal decision.
          if ((approval.revision ?? 0) < (previous.revision ?? 0)) return old;
          const merged = {
            ...previous,
            ...approval,
            live: true,
            arguments: approval.arguments ?? previous.arguments
          };
          return [...old.slice(0, index), merged, ...old.slice(index + 1)];
        });
      });

      socket.on('question', (question: IQuestion) => {
        setQuestions((old) => {
          const index = old.findIndex((item) => item.id === question.id);
          if (index === -1) return [...old, { ...question, live: true }];
          const previous = old[index];
          // A late pending event must not overwrite a terminal answer.
          if ((question.revision ?? 0) < (previous.revision ?? 0)) return old;
          return [
            ...old.slice(0, index),
            { ...previous, ...question, live: true },
            ...old.slice(index + 1)
          ];
        });
      });

      socket.on('remove_action', (action: IAction) => {
        setActions((old) => {
          const index = old.findIndex((a) => a.id === action.id);
          if (index === -1) return old;
          return [...old.slice(0, index), ...old.slice(index + 1)];
        });
      });

      socket.on('token_usage', (count: number) => {
        setTokenCount((old) => old + count);
      });

      socket.on('window_message', (data: any) => {
        if (window.parent) {
          window.parent.postMessage(data, '*');
        }
      });

      socket.on('toast', (data: { message: string; type: string }) => {
        if (!data.message) {
          console.warn('No message received for toast.');
          return;
        }

        switch (data.type) {
          case 'info':
            toast.info(data.message);
            break;
          case 'error':
            toast.error(data.message);
            break;
          case 'success':
            toast.success(data.message);
            break;
          case 'warning':
            toast.warning(data.message);
            break;
          default:
            toast(data.message);
            break;
        }
      });
    },
    [setSession, sessionId, idToResume, chatProfile, config]
  );

  // Keep a single debounced connect for the lifetime of the session. A new
  // debounce per _connect version left older pending connects alive, so a
  // transient "/" connect could open after a thread resume and win the race.
  const connectRef = useRef(_connect);
  useEffect(() => {
    connectRef.current = _connect;
  }, [_connect]);
  const connect = useMemo(
    () =>
      debounce(
        (args: Parameters<typeof _connect>[0]) => connectRef.current(args),
        200
      ),
    []
  );

  const disconnect = useCallback(() => {
    if (session?.socket) {
      session.socket.removeAllListeners();
      session.socket.close();
    }
  }, [session]);

  return {
    connect,
    disconnect,
    session,
    sessionId,
    chatProfile,
    idToResume,
    setChatProfile
  };
};

export { questionFromStep, useChatSession };
