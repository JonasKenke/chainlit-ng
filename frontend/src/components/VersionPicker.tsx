import { ChevronDown } from 'lucide-react';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu';

export interface VersionOption {
  id: string;
  version: number;
  createdAt?: string;
}

interface Props {
  value: string;
  options: VersionOption[];
  onChange: (id: string) => void;
  label: string;
  latestId?: string;
  currentId?: string;
  testId?: string;
}

function formatDate(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString();
}

/** Compact, dated version picker shared by the prompt and skill panels. */
export function VersionPicker({
  value,
  options,
  onChange,
  label,
  latestId,
  currentId,
  testId = 'version-picker'
}: Props) {
  const { t } = useTranslation();
  const selected = options.find((option) => option.id === value) ?? options[0];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 min-w-24 justify-between gap-2 px-2 text-xs"
          aria-label={label}
          data-testid={testId}
        >
          <span className="font-medium">
            {selected ? `v${selected.version}` : '—'}
          </span>
          {selected?.createdAt ? (
            <span className="hidden text-muted-foreground sm:inline">
              {formatDate(selected.createdAt)}
            </span>
          ) : null}
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-60">
        <DropdownMenuRadioGroup value={value} onValueChange={onChange}>
          {options.map((option) => (
            <DropdownMenuRadioItem
              key={option.id}
              value={option.id}
              data-testid={`${testId}-option-${option.id}`}
            >
              <span className="flex min-w-0 flex-1 items-center justify-between gap-2">
                <span className="font-medium">v{option.version}</span>
                {option.id === currentId ? (
                  <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                    {t('common.current')}
                  </span>
                ) : option.id === latestId ? (
                  <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                    {t('common.latest')}
                  </span>
                ) : null}
                <span className="ml-auto text-xs text-muted-foreground">
                  {formatDate(option.createdAt)}
                </span>
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export default VersionPicker;
