import { renderHook } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import type { IMessageElement } from '@chainlit/react-client';

import { useSideElements } from '@/components/chat/MessagesContainer/useSideElements';

const element = (id: string, autoExpand?: boolean): IMessageElement => ({
  id,
  name: id,
  type: 'text',
  display: 'side',
  forId: 'message',
  url: 'https://example.com/text',
  autoExpand
});

function renderPanel(elements: IMessageElement[]) {
  return renderHook(
    ({ elements }) => {
      const [panel, setPanel] = useState<{
        title: string;
        elements: IMessageElement[];
        key?: string;
      }>();
      useSideElements(elements, setPanel);
      return {
        panel,
        setPanel,
        close: () => setPanel(undefined),
        open: (el: IMessageElement) =>
          setPanel({ title: el.name, elements: [el] })
      };
    },
    { initialProps: { elements } }
  );
}

describe('useSideElements', () => {
  it('opens new side elements by default', () => {
    const { result } = renderPanel([element('source')]);

    expect(result.current.panel?.title).toBe('source');
    expect(result.current.panel?.elements).toEqual([element('source')]);
  });

  it('keeps a closed panel closed while opted-out elements arrive or update', () => {
    const first = element('first');
    const { result, rerender } = renderPanel([first]);
    result.current.close();

    const second = element('second', false);
    rerender({ elements: [first, second] });
    expect(result.current.panel).toBeUndefined();

    rerender({ elements: [first, { ...second, name: 'updated' }] });
    expect(result.current.panel).toBeUndefined();
  });

  it('updates an already open panel for an opted-out element', () => {
    const quiet = element('quiet', false);
    const { result, rerender } = renderPanel([quiet]);
    expect(result.current.panel).toBeUndefined();

    result.current.open(quiet);
    const updated = { ...quiet, name: 'updated' };
    rerender({ elements: [updated] });

    expect(result.current.panel?.elements).toEqual([updated]);
    expect(result.current.panel?.title).toBe('updated');
  });

  it('opens a mixed batch when a changed element opts in', () => {
    const { result, rerender } = renderPanel([]);

    rerender({ elements: [element('quiet', false), element('default')] });

    expect(result.current.panel?.elements).toHaveLength(2);
  });

  it('keeps reopening on unflagged updates', () => {
    const first = element('first');
    const { result, rerender } = renderPanel([first]);
    result.current.close();

    rerender({ elements: [{ ...first, name: 'updated' }] });

    expect(result.current.panel?.title).toBe('updated');
  });

  it('clears the panel when no side elements remain', () => {
    const { result, rerender } = renderPanel([element('first')]);

    rerender({ elements: [{ ...element('inline'), display: 'inline' }] });

    expect(result.current.panel).toBeUndefined();
  });
});
