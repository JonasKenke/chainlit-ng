import { cn } from '@/lib/utils';
import { Library } from 'lucide-react';
import { useContext, useState } from 'react';
import { toast } from 'sonner';

import {
  ChainlitContext,
  IMcpPrompt,
  IMcpResource,
  useChainlitStore
} from '@chainlit/react-client';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';
import { useTranslation } from 'components/i18n/Translator';

interface Props {
  disabled?: boolean;
  onInsertPrompt: (text: string) => void;
}

type ServerPrompt = IMcpPrompt & { server: string };
type ServerResource = IMcpResource & { server: string };

const errorDetail = (reason: unknown, fallback: string) =>
  (reason as { detail?: string } | undefined)?.detail || fallback;

const itemKey = (server: string, id: string) => `${server}:${id}`;

/**
 * Prompts and resources of every connected MCP server in one picker.
 *
 * Selecting a prompt renders it into the composer; it is never sent by
 * itself. Selecting a resource asks the backend to read it and show the
 * resulting element in the chat.
 */
export function McpLibraryButton({ disabled, onInsertPrompt }: Props) {
  const { t } = useTranslation();
  const apiClient = useContext(ChainlitContext);
  const sessionId = useChainlitStore((state) => state.sessionId);
  const mcps = useChainlitStore((state) => state.mcps);

  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<'prompts' | 'resources'>('prompts');
  const [selected, setSelected] = useState<ServerPrompt | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const connected = mcps.filter((mcp) => mcp.status === 'connected');
  const prompts: ServerPrompt[] = connected.flatMap((mcp) =>
    (mcp.prompts ?? []).map((prompt) => ({ ...prompt, server: mcp.name }))
  );
  const resources: ServerResource[] = connected.flatMap((mcp) =>
    (mcp.resources ?? []).map((resource) => ({ ...resource, server: mcp.name }))
  );

  if (!prompts.length && !resources.length) return null;

  const close = () => {
    setOpen(false);
    setSelected(null);
    setValues({});
  };

  const invokePrompt = async (
    item: ServerPrompt,
    args: Record<string, string>
  ) => {
    setBusy(itemKey(item.server, item.name));
    try {
      const { text } = await apiClient.getMcpPrompt(
        sessionId,
        item.server,
        item.name,
        args
      );
      onInsertPrompt(text);
      toast.success(t('chat.mcp.library.promptInserted'));
      close();
    } catch (reason) {
      toast.error(errorDetail(reason, t('chat.mcp.library.promptFailed')));
    } finally {
      setBusy(null);
    }
  };

  const selectPrompt = (item: ServerPrompt) => {
    const args = Object.fromEntries(
      (item.arguments ?? []).map((argument) => [argument.name, ''])
    );
    setValues(args);
    // Any declared argument gets a form, so optional ones can be filled too.
    if (item.arguments?.length) {
      setSelected(item);
      return;
    }
    void invokePrompt(item, args);
  };

  const readResource = async (item: ServerResource) => {
    setBusy(itemKey(item.server, item.uri));
    try {
      await apiClient.readMcpResource(
        sessionId,
        item.server,
        item.uri,
        item.name
      );
      toast.success(t('chat.mcp.library.resourceAdded'));
      close();
    } catch (reason) {
      toast.error(errorDetail(reason, t('chat.mcp.library.resourceFailed')));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) {
          setSelected(null);
          setValues({});
        }
      }}
    >
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              id="mcp-library-open"
              variant="ghost"
              size="icon"
              className={cn(
                'rounded-full hover:bg-muted hover:dark:bg-muted transition-all duration-200',
                open && 'bg-muted/50'
              )}
              disabled={disabled}
              onClick={() => setOpen(true)}
            >
              <Library className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <p>{t('chat.mcp.library.button')}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <DialogContent
        id="mcp-library"
        className="sm:max-w-2xl flex flex-col max-h-[80vh] gap-4"
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle>
            {selected ? selected.name : t('chat.mcp.library.title')}
          </DialogTitle>
        </DialogHeader>

        {selected ? (
          <form
            className="flex flex-col gap-3 overflow-y-auto"
            onSubmit={(event) => {
              event.preventDefault();
              void invokePrompt(selected, values);
            }}
          >
            {selected.description ? (
              <p className="text-sm text-muted-foreground">
                {selected.description}
              </p>
            ) : null}
            {(selected.arguments ?? []).map((argument) => (
              <div key={argument.name} className="flex flex-col gap-1">
                <Label htmlFor={`mcp-prompt-arg-${argument.name}`}>
                  {argument.name}
                  {argument.required ? ' *' : ''}
                </Label>
                {argument.description ? (
                  <span className="text-xs text-muted-foreground">
                    {argument.description}
                  </span>
                ) : null}
                <Input
                  id={`mcp-prompt-arg-${argument.name}`}
                  required={argument.required}
                  value={values[argument.name] ?? ''}
                  onChange={(event) =>
                    setValues((previous) => ({
                      ...previous,
                      [argument.name]: event.target.value
                    }))
                  }
                />
              </div>
            ))}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setSelected(null)}
              >
                {t('common.actions.cancel')}
              </Button>
              <Button type="submit" disabled={!!busy}>
                {t('chat.mcp.library.promptInsert')}
              </Button>
            </div>
          </form>
        ) : (
          <Tabs
            value={tab}
            onValueChange={(value) => setTab(value as 'prompts' | 'resources')}
            className="flex-1 overflow-hidden flex flex-col"
          >
            <TabsList className="self-start">
              <TabsTrigger value="prompts">
                {t('chat.mcp.library.tabs.prompts')}
              </TabsTrigger>
              <TabsTrigger value="resources">
                {t('chat.mcp.library.tabs.resources')}
              </TabsTrigger>
            </TabsList>

            <TabsContent
              value="prompts"
              className="flex-1 overflow-y-auto custom-scrollbar flex flex-col gap-2"
            >
              {prompts.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  {t('chat.mcp.library.prompts.empty')}
                </p>
              ) : (
                prompts.map((prompt) => (
                  <button
                    type="button"
                    key={itemKey(prompt.server, prompt.name)}
                    disabled={!!busy}
                    onClick={() => selectPrompt(prompt)}
                    className="text-left border rounded-md p-3 hover:bg-muted transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">{prompt.name}</span>
                      <Badge variant="outline">{prompt.server}</Badge>
                    </div>
                    {prompt.description ? (
                      <span className="text-xs text-muted-foreground line-clamp-2">
                        {prompt.description}
                      </span>
                    ) : null}
                  </button>
                ))
              )}
            </TabsContent>

            <TabsContent
              value="resources"
              className="flex-1 overflow-y-auto custom-scrollbar flex flex-col gap-2"
            >
              {resources.length === 0 ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  {t('chat.mcp.library.resources.empty')}
                </p>
              ) : (
                resources.map((resource) => (
                  <button
                    type="button"
                    key={itemKey(resource.server, resource.uri)}
                    disabled={!!busy}
                    onClick={() => void readResource(resource)}
                    className="text-left border rounded-md p-3 hover:bg-muted transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">
                        {resource.name}
                      </span>
                      <Badge variant="outline">{resource.server}</Badge>
                    </div>
                    <span className="text-xs text-muted-foreground break-all line-clamp-2">
                      {resource.uri}
                    </span>
                  </button>
                ))
              )}
            </TabsContent>
          </Tabs>
        )}
      </DialogContent>
    </Dialog>
  );
}
