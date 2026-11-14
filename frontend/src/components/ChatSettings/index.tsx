import {
  useChatData,
  useChatInteract,
  useSetChainlitState
} from '@chainlit-ng/react-client';
import cloneDeep from 'lodash/cloneDeep';
import mapValues from 'lodash/mapValues';
import { useCallback, useEffect } from 'react';
import { useForm } from 'react-hook-form';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Translator } from 'components/i18n';

import { useAppStore } from '@/state/store';

import { FormInput, TFormInputValue } from './FormInput';
import { effectiveItems, resolveDependentValues } from './dependentInputs';
import { useChatSettingsSnapshotAtOpen } from './useChatSettingsSnapshotAtOpen';

export default function ChatSettingsModal() {
  const { chatSettingsValue, chatSettingsInputs } = useChatData();

  const { updateChatSettings, editChatSettings } = useChatInteract();
  const chatSettingsOpen = useAppStore((state) => state.chatSettingsOpen);
  const setChatSettingsOpen = useAppStore((state) => state.setChatSettingsOpen);

  const { valuesAtOpen, inputsAtOpen } = useChatSettingsSnapshotAtOpen(
    chatSettingsOpen,
    chatSettingsValue,
    chatSettingsInputs
  );

  const { handleSubmit, setValue, reset, watch, getValues } = useForm({
    defaultValues: chatSettingsValue
  });
  const setChatSettingsValue = useSetChainlitState('chatSettingsValue');
  const setChatSettingsInputs = useSetChainlitState('chatSettingsInputs');

  const restoreSnapshot = useCallback(() => {
    setChatSettingsInputs(cloneDeep(inputsAtOpen));
    setChatSettingsValue(cloneDeep(valuesAtOpen));
  }, [inputsAtOpen, setChatSettingsInputs, setChatSettingsValue, valuesAtOpen]);

  // Reset form when default values change
  useEffect(() => {
    reset(chatSettingsValue);
  }, [chatSettingsValue, reset]);

  const handleClose = (open: boolean) => {
    if (!open) {
      restoreSnapshot();
      setChatSettingsOpen(false);
    }
  };

  const handleConfirm = handleSubmit((data) => {
    const reconciledValues = resolveDependentValues(chatSettingsInputs, data);
    const processedValues = mapValues(reconciledValues, (x: TFormInputValue) =>
      x !== '' ? x : null
    );
    updateChatSettings(processedValues);
    setChatSettingsValue(processedValues);
    setChatSettingsOpen(false);
  });

  const handleReset = () => {
    restoreSnapshot();
  };

  // Legacy setField compatibility layer
  const handleChange = () => {};

  const setFieldValue = (field: string, value: any) => {
    setValue(field, value);
    const currentValues = getValues();
    const resolvedValues = resolveDependentValues(
      chatSettingsInputs,
      currentValues
    );
    Object.entries(resolvedValues).forEach(([key, resolvedValue]) => {
      if (resolvedValue !== currentValues[key]) {
        setValue(key, resolvedValue);
      }
    });
    editChatSettings(resolvedValues);
  };

  const values = watch();
  const resolvedValues = resolveDependentValues(chatSettingsInputs, values);

  const getElement = (input: any) => ({
    ...input,
    ...(input.depends_on
      ? { items: effectiveItems(input, resolvedValues[input.depends_on]) }
      : {}),
    value: resolvedValues[input.id],
    onChange: handleChange,
    setField: setFieldValue
  });

  const tabInputs = chatSettingsInputs.filter(
    (input: any) => Array.isArray(input?.inputs) && input.inputs.length > 0
  );
  const regularInputs = chatSettingsInputs.filter(
    (input: any) => !Array.isArray(input?.inputs) || input.inputs.length === 0
  );
  const hasTabs = tabInputs.length > 0;
  const defaultTab = tabInputs[0]?.id;

  return (
    <Dialog open={chatSettingsOpen} onOpenChange={handleClose}>
      <DialogContent
        id="chat-settings"
        className={`flex flex-col gap-6 p-6 ${
          hasTabs ? 'min-w-[25vw] h-[85vh]' : 'min-w-[20vw] max-h-[85vh]'
        }`}
      >
        <DialogHeader>
          <DialogTitle>
            <Translator path="chat.settings.title" />
          </DialogTitle>
          <DialogDescription className="sr-only">
            <Translator path="chat.settings.customize" />
          </DialogDescription>
        </DialogHeader>
        {hasTabs ? (
          <Tabs
            defaultValue={defaultTab}
            className="flex flex-col flex-grow min-h-0"
          >
            <TabsList className="w-full flex justify-start">
              {tabInputs.map((tab: any) => (
                <TabsTrigger key={tab.id} value={tab.id}>
                  {tab.label ?? tab.id}
                </TabsTrigger>
              ))}
            </TabsList>
            {tabInputs.map((tab: any) => (
              <TabsContent
                key={tab.id}
                value={tab.id}
                className="data-[state=active]:flex flex-col flex-grow overflow-y-auto gap-6 p-1 mt-4"
              >
                {tab.inputs?.map((input: any) => (
                  <FormInput key={input.id} element={getElement(input)} />
                ))}
              </TabsContent>
            ))}
          </Tabs>
        ) : (
          <div className="flex flex-col flex-grow overflow-y-auto gap-6 p-1">
            {regularInputs.map((input: any) => (
              <FormInput key={input.id} element={getElement(input)} />
            ))}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={handleReset}>
            <Translator path="common.actions.reset" />
          </Button>
          <div className="flex-1" />
          <Button variant="ghost" onClick={() => handleClose(false)}>
            <Translator path="common.actions.cancel" />
          </Button>
          <Button onClick={handleConfirm} id="confirm" autoFocus>
            <Translator path="common.actions.confirm" />
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
