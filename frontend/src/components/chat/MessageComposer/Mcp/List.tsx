import { cn } from '@/lib/utils';
import {
  ChainlitContext,
  IMcp,
  sanitizeMcpUrl,
  useChainlitState,
  useChainlitStore
} from '@chainlit-ng/react-client';
import { Link, LogIn, RefreshCw, Trash2, Wrench } from 'lucide-react';
import { useContext, useState } from 'react';
import { toast } from 'sonner';

import CopyButton from '@/components/CopyButton';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useTranslation } from 'components/i18n/Translator';

import {
  getAuthorizationLink,
  openAuthorizationUrl
} from './authorizationLink';

interface McpListProps {
  onAddNewClick: () => void;
}

export const McpList = ({ onAddNewClick }: McpListProps) => {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const [mcps, setMcps] = useChainlitState('mcps');
  const [isLoading, setIsLoading] = useState(false);
  const { t } = useTranslation();

  const deleteMcp = (mcp: IMcp) => {
    // A pending sign-in has no McpSession yet, but the backend still has to
    // cancel the flow and forget the credential, so ask before dropping it.
    if (mcp.status === 'connected' || mcp.authorizationUrl) {
      setIsLoading(true);

      toast.promise(
        apiClient
          .disconnectMcp(sessionId, mcp.name)
          .then(() => {})
          .finally(() => setIsLoading(false)),
        {
          loading: t('chat.mcp.messages.removing'),
          success: () => t('chat.mcp.messages.removed'),
          error: (err) => <span>{err.message}</span>
        }
      );
    }

    setMcps((prev) => prev.filter((_mcp) => _mcp.name !== mcp.name));
  };

  if (!mcps || mcps.length === 0) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        <p>{t('chat.mcp.list.empty')}</p>
        <Button variant="outline" className="mt-4" onClick={onAddNewClick}>
          {t('chat.mcp.list.addFirst')}
        </Button>
      </div>
    );
  }

  return (
    <>
      {mcps.map((mcp, index) => (
        <McpItem
          key={index}
          mcp={mcp}
          onDelete={deleteMcp}
          isLoading={isLoading}
        />
      ))}
    </>
  );
};

interface McpItemProps {
  mcp: IMcp;
  onDelete: (mcp: IMcp) => void;
  isLoading: boolean;
}

const McpItem = ({ mcp, onDelete, isLoading }: McpItemProps) => {
  const { t } = useTranslation();
  const transport = mcp.type || mcp.clientType;
  const displayUrl = sanitizeMcpUrl(mcp.url);

  return (
    <div className="border rounded-lg p-4 flex flex-col gap-3">
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-2">
          <div
            className={cn(
              'h-2 w-2 rounded-full',
              mcp.status === 'connected' && 'bg-green-500',
              mcp.status === 'connecting' && 'bg-yellow-500',
              mcp.status === 'failed' && 'bg-red-500'
            )}
          />
          <h3 className="font-medium">{mcp.name}</h3>
          <Badge variant="outline">{transport}</Badge>
        </div>
        <div className="flex items-center">
          <ReconnectMcpButton mcp={mcp} />
          <DeleteMcpButton mcp={mcp} onDelete={onDelete} disabled={isLoading} />
        </div>
      </div>

      {mcp.authorizationUrl && <OAuthSignIn url={mcp.authorizationUrl} />}

      {mcp.detail && <p className="text-sm text-destructive">{mcp.detail}</p>}

      {mcp.isUserProvided && displayUrl && (
        <div className="flex gap-2 flex-wrap">
          <div className="font-medium text-sm text-muted-foreground flex items-center">
            <Link className="h-4 w-4 mr-2" />
            {t('chat.mcp.list.url')}
          </div>
          <div className="flex items-center w-full bg-accent px-3 py-1 rounded gap-2">
            <pre className="text-sm font-mono flex-grow truncate">
              {displayUrl}
            </pre>
            <CopyButton content={displayUrl} />
          </div>
        </div>
      )}

      <div className="font-medium text-sm text-muted-foreground flex items-center">
        <Wrench className="h-4 w-4 mr-2" />
        {t('chat.mcp.list.tools')}
      </div>
      <div className="flex flex-wrap gap-2">
        {mcp.tools &&
          mcp.tools.map((tool, toolIndex) => (
            <Badge key={toolIndex} variant="secondary">
              {tool.name}
            </Badge>
          ))}
      </div>
    </div>
  );
};

const OAuthSignIn = ({ url }: { url: string }) => {
  const { t } = useTranslation();
  const link = getAuthorizationLink(url);

  if (!link) return null;

  return (
    <a
      href={link.href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-sm underline flex items-center gap-2 w-fit"
      onClick={(event) => {
        // Never navigate the app itself; open the AS in a new tab on click.
        event.preventDefault();
        openAuthorizationUrl(link.href);
      }}
    >
      <LogIn className="h-4 w-4" />
      {t('chat.mcp.list.signIn', { host: link.host })}
    </a>
  );
};

interface DeleteMcpButtonProps {
  mcp: IMcp;
  onDelete: (mcp: IMcp) => void;
  disabled: boolean;
}

const DeleteMcpButton = ({ mcp, onDelete, disabled }: DeleteMcpButtonProps) => {
  const { t } = useTranslation();

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="text-destructive"
          aria-label={t('chat.mcp.list.remove')}
          disabled={disabled}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('chat.mcp.list.removeTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('chat.mcp.list.removeDescription', { name: mcp.name })}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('common.actions.cancel')}</AlertDialogCancel>
          <AlertDialogAction
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            onClick={() => onDelete(mcp)}
          >
            {t('common.actions.confirm')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
};

const ReconnectMcpButton = ({ mcp }: { mcp: IMcp }) => {
  const apiClient = useContext(ChainlitContext);
  const [, setMcps] = useChainlitState('mcps');
  const sessionId = useChainlitStore((state) => state.sessionId);
  const [isLoading, setIsLoading] = useState(false);
  const { t } = useTranslation();

  const reconnectMcp = () => {
    setIsLoading(true);

    setMcps((prev) =>
      prev.map((existingMcp) => {
        if (existingMcp.name === mcp.name) {
          return {
            ...existingMcp,
            status: 'connecting'
          };
        }
        return existingMcp;
      })
    );

    const updateMcpStatus = (success: boolean, updatedMcp?: any) => {
      setMcps((prev) =>
        prev.map((existingMcp) => {
          if (existingMcp.name === mcp.name) {
            return {
              ...existingMcp,
              status: success ? 'connected' : 'failed',
              tools: updatedMcp ? updatedMcp.tools : existingMcp.tools
            };
          }
          return existingMcp;
        })
      );
    };

    const promise =
      mcp.isUserProvided && mcp.clientType && mcp.url
        ? apiClient.connectUserMcp(
            sessionId,
            mcp.name,
            mcp.clientType,
            mcp.url,
            mcp.headers
          )
        : apiClient.connectMcp(sessionId, mcp.name);

    toast.promise(
      promise
        .then((resp: any) => {
          const { success, mcp: updatedMcp } = resp;
          updateMcpStatus(success, updatedMcp);
        })
        .catch(() => {
          updateMcpStatus(false);
        })
        .finally(() => setIsLoading(false)),
      {
        loading: t('chat.mcp.messages.reconnecting'),
        success: () => t('chat.mcp.messages.reconnected'),
        error: (err) => <span>{err.message}</span>
      }
    );
  };

  return (
    <Button
      variant="ghost"
      size="icon"
      disabled={isLoading}
      onClick={reconnectMcp}
    >
      <RefreshCw className="h-4 w-4" />
    </Button>
  );
};
