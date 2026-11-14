import {
  ChainlitContext,
  IMcp,
  useChainlitState,
  useChainlitStore
} from '@chainlit-ng/react-client';
import { useContext, useState } from 'react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/select';
import { useTranslation } from 'components/i18n/Translator';

interface McpServerConfig {
  name: string;
  type: string;
}

interface McpAddFormProps {
  servers: McpServerConfig[];
  userServersEnabled: boolean;
  onSuccess: () => void;
  onCancel: () => void;
}

export const McpAddForm = ({
  servers,
  userServersEnabled,
  onSuccess,
  onCancel
}: McpAddFormProps) => {
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const [mcps, setMcps] = useChainlitState('mcps');
  const { t } = useTranslation();

  const [serverName, setServerName] = useState('');
  const [serverType, setServerType] = useState<'sse' | 'streamable-http'>(
    'sse'
  );
  const [serverUrl, setServerUrl] = useState('');
  const [headersInput, setHeadersInput] = useState('');
  const [useOAuth, setUseOAuth] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  const upsertMcp = (incoming: IMcp) => {
    setMcps((prev) => {
      if (!prev.some((mcp) => mcp.name === incoming.name)) {
        return [...prev, incoming];
      }
      return prev.map((mcp) =>
        mcp.name === incoming.name ? { ...mcp, ...incoming } : mcp
      );
    });
  };

  const errorText = () => t('chat.mcp.errors.connect');

  const connectConfigured = (server: McpServerConfig) => {
    setIsLoading(true);
    toast.promise(
      apiClient
        .connectMcp(sessionId, server.name)
        .then((resp: any) => {
          if (!resp?.success) {
            throw new Error(resp?.error || t('chat.mcp.errors.connect'));
          }
          upsertMcp({ ...resp.mcp, status: 'connected' });
          onSuccess();
        })
        .finally(() => setIsLoading(false)),
      {
        loading: t('chat.mcp.messages.connecting'),
        success: t('chat.mcp.messages.connected'),
        error: errorText
      }
    );
  };

  const isFormValid = () => !!serverName.trim() && !!serverUrl.trim();

  const resetForm = () => {
    setServerName('');
    setServerType('sse');
    setServerUrl('');
    setHeadersInput('');
    setUseOAuth(false);
  };

  const addUserMcp = () => {
    // OAuth is streamable-HTTP only; the backend rejects it on SSE and stdio.
    const optInOAuth = useOAuth && serverType === 'streamable-http';
    let headers: Record<string, string> | undefined;
    if (headersInput.trim()) {
      try {
        const parsed = JSON.parse(headersInput.trim());
        if (
          !parsed ||
          typeof parsed !== 'object' ||
          Array.isArray(parsed) ||
          !Object.values(parsed).every((value) => typeof value === 'string')
        ) {
          throw new Error('Invalid headers');
        }
        headers = parsed as Record<string, string>;
      } catch {
        toast.error(t('chat.mcp.form.headersInvalid'));
        return;
      }
    }

    setIsLoading(true);
    let authorizationRequired = false;
    toast.promise(
      apiClient
        .connectUserMcp(
          sessionId,
          serverName.trim(),
          serverType,
          serverUrl.trim(),
          headers,
          optInOAuth
        )
        .then((resp: any) => {
          if (resp?.status === 'authorization_required') {
            // A 202 is a pending sign-in, not a failure: keep the row so the
            // user can open the authorization URL or remove the attempt.
            authorizationRequired = true;
            upsertMcp({
              ...resp.mcp,
              name: serverName.trim(),
              status: 'connecting',
              isUserProvided: true,
              clientType: serverType,
              url: serverUrl.trim(),
              headers,
              useOAuth: true,
              authorizationUrl: resp.url
            });
          } else {
            if (!resp?.success) {
              throw new Error(resp?.error || t('chat.mcp.errors.connect'));
            }
            upsertMcp({
              ...resp.mcp,
              status: 'connected',
              isUserProvided: true,
              clientType: serverType,
              url: serverUrl.trim(),
              headers,
              useOAuth: optInOAuth
            });
          }
          resetForm();
          onSuccess();
        })
        .finally(() => setIsLoading(false)),
      {
        loading: t('chat.mcp.messages.adding'),
        success: () =>
          authorizationRequired
            ? t('chat.mcp.messages.authorizationRequired')
            : t('chat.mcp.messages.added'),
        error: (err: any) => err?.detail || err?.message || errorText()
      }
    );
  };

  return (
    <>
      <div className="flex flex-col gap-6">
        {servers.length > 0 && (
          <div className="flex flex-col gap-2">
            <Label className="text-foreground/70 text-sm">
              {t('chat.mcp.configured.title')}
            </Label>
            {servers.map((server) => {
              const status = mcps.find(
                (mcp) => mcp.name === server.name
              )?.status;
              return (
                <div
                  key={server.name}
                  className="border rounded-lg p-3 flex items-center justify-between gap-2"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{server.name}</span>
                    <Badge variant="outline">{server.type}</Badge>
                  </div>
                  <Button
                    size="sm"
                    disabled={isLoading || status === 'connecting'}
                    onClick={() => connectConfigured(server)}
                  >
                    {status === 'connected'
                      ? t('chat.mcp.reconnect')
                      : t('chat.mcp.connect')}
                  </Button>
                </div>
              );
            })}
          </div>
        )}

        {userServersEnabled && (
          <div className="flex flex-col gap-4">
            <Label className="text-foreground/70 text-sm">
              {t('chat.mcp.add.title')}
            </Label>
            <div className="flex gap-2 w-full">
              <div className="flex flex-col flex-grow gap-2">
                <Label
                  htmlFor="server-name"
                  className="text-foreground/70 text-sm"
                >
                  {t('chat.mcp.form.name')} *
                </Label>
                <Input
                  id="server-name"
                  placeholder={t('chat.mcp.form.namePlaceholder')}
                  className="w-full bg-background text-foreground border-input"
                  value={serverName}
                  onChange={(e) => setServerName(e.target.value)}
                  required
                  disabled={isLoading}
                />
              </div>

              <div className="flex flex-col gap-2">
                <Label
                  htmlFor="server-type"
                  className="text-foreground/70 text-sm"
                >
                  {t('chat.mcp.form.type')} *
                </Label>
                <Select
                  value={serverType}
                  onValueChange={(value) => {
                    const nextType = value as 'sse' | 'streamable-http';
                    setServerType(nextType);
                    if (nextType !== 'streamable-http') setUseOAuth(false);
                  }}
                  disabled={isLoading}
                >
                  <SelectTrigger
                    id="server-type"
                    className="w-full bg-background text-foreground border-input"
                  >
                    <SelectValue placeholder={t('chat.mcp.form.type')} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="sse">sse</SelectItem>
                    <SelectItem value="streamable-http">
                      streamable-http
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <Label
                htmlFor="server-url"
                className="text-foreground/70 text-sm"
              >
                {t('chat.mcp.form.url')} *
              </Label>
              <Input
                id="server-url"
                placeholder={t('chat.mcp.form.urlPlaceholder')}
                className="w-full bg-background text-foreground border-input"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                required
                disabled={isLoading}
              />
            </div>

            {serverType === 'streamable-http' && (
              <div className="flex items-center gap-2">
                <Checkbox
                  id="server-oauth"
                  checked={useOAuth}
                  onCheckedChange={(checked) => setUseOAuth(checked === true)}
                  disabled={isLoading}
                />
                <Label
                  htmlFor="server-oauth"
                  className="text-foreground/70 text-sm"
                >
                  {t('chat.mcp.form.useOAuth')}
                </Label>
              </div>
            )}

            <div className="flex flex-col gap-2">
              <Label htmlFor="headers" className="text-foreground/70 text-sm">
                {t('chat.mcp.form.headers')}
              </Label>
              <Input
                id="headers"
                placeholder={t('chat.mcp.form.headersPlaceholder')}
                className="w-full bg-background text-foreground border-input font-mono"
                value={headersInput}
                onChange={(e) => setHeadersInput(e.target.value)}
                disabled={isLoading}
              />
            </div>
          </div>
        )}
      </div>

      {userServersEnabled && (
        <div className="flex justify-end items-center gap-2 mt-auto">
          <Button variant="outline" onClick={onCancel} disabled={isLoading}>
            {t('common.actions.cancel')}
          </Button>
          <Button
            variant="default"
            onClick={addUserMcp}
            disabled={!isFormValid() || isLoading}
          >
            {t('chat.mcp.form.add')}
          </Button>
        </div>
      )}
    </>
  );
};
