import {
  ChainlitContext,
  ClientError,
  useAuth,
  useChatInteract,
  useChatMessages,
  useChatSession,
  useConfig,
  useSetChainlitState
} from '@chainlit-ng/react-client';
import capitalize from 'lodash/capitalize';
import { LogOut, Trash2 } from 'lucide-react';
import { useContext, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle
} from '@/components/ui/alert-dialog';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Button, buttonVariants } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu';
import { Translator, useTranslation } from 'components/i18n';

const THREAD_HISTORY_PAGE_SIZE = 35;

export default function UserNav() {
  const { user, logout } = useAuth();
  const { config } = useConfig();
  const apiClient = useContext(ChainlitContext);
  const { clear } = useChatInteract();
  const { threadId } = useChatMessages();
  const { idToResume } = useChatSession();
  const setThreadHistory = useSetChainlitState('threadHistory');
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [isClearAllOpen, setIsClearAllOpen] = useState(false);

  if (!user) return null;
  const displayName = user?.display_name || user?.identifier;
  const canClearAll = Boolean(config?.threadClearAll);

  const refetchThreadHistory = async () => {
    try {
      const { pageInfo, data } = await apiClient.listThreads(
        { first: THREAD_HISTORY_PAGE_SIZE },
        {}
      );
      setThreadHistory({ threads: data, pageInfo });
    } catch {
      // The failed deletion is already reported; keep the current history.
    }
  };

  const handleClearAll = async () => {
    if (threadId || idToResume) {
      // Leave the thread route before the request: AutoResumeThread would
      // otherwise re-select a thread that is about to be deleted.
      clear();
      navigate('/');
      await new Promise((resolve) => setTimeout(resolve, 300));
    }

    toast.promise(apiClient.deleteAllThreads(), {
      loading: t('threadHistory.clearAll.inProgress'),
      success: () => {
        setThreadHistory({
          threads: [],
          timeGroupedThreads: {},
          pageInfo: undefined
        });
        return t('threadHistory.clearAll.success');
      },
      error: (err) => {
        refetchThreadHistory();
        return err instanceof ClientError
          ? err.message
          : t('threadHistory.clearAll.error');
      }
    });
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            id="user-nav-button"
            variant="ghost"
            className="relative h-8 w-8 rounded-full"
          >
            <Avatar className="h-8 w-8">
              <AvatarImage src={user?.metadata.image} alt="user image" />
              <AvatarFallback className="bg-primary text-primary-foreground font-semibold">
                {capitalize(displayName[0])}
              </AvatarFallback>
            </Avatar>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          className="max-w-[min(24rem,calc(100vw-2rem))]"
          align="end"
          forceMount
        >
          <DropdownMenuLabel className="font-normal">
            <div className="flex flex-col space-y-1">
              <p className="wrap-anywhere text-sm font-medium leading-none">
                {displayName}
              </p>
            </div>
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          {canClearAll && (
            <DropdownMenuItem
              className="text-red-600 focus:bg-red-600/10 focus:text-red-600 dark:text-red-400 dark:focus:text-red-400"
              onSelect={() => setIsClearAllOpen(true)}
            >
              <Translator path="navigation.user.menu.clearAll" />
              <Trash2 className="ml-auto" />
            </DropdownMenuItem>
          )}
          <DropdownMenuItem onClick={() => logout(true)}>
            <Translator path="navigation.user.menu.logout" />
            <LogOut className="ml-auto" />
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {canClearAll && (
        <AlertDialog open={isClearAllOpen} onOpenChange={setIsClearAllOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle className="text-destructive">
                <Translator path="threadHistory.clearAll.title" />
              </AlertDialogTitle>
              <AlertDialogDescription>
                <Translator path="threadHistory.clearAll.description" />
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter className="flex-row gap-2 sm:gap-0">
              <AlertDialogCancel className="mt-0">
                <Translator path="common.actions.cancel" />
              </AlertDialogCancel>
              <AlertDialogAction
                className={buttonVariants({ variant: 'destructive' })}
                onClick={handleClearAll}
              >
                <Translator path="threadHistory.clearAll.confirm" />
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </>
  );
}
