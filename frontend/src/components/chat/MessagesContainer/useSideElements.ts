import { useEffect, useRef } from 'react';

import type { IMessageElement } from '@chainlit/react-client';

type SideView = {
  title: string;
  elements: IMessageElement[];
  key?: string;
};
type SetSideView = (
  update:
    | SideView
    | undefined
    | ((current: SideView | undefined) => SideView | undefined)
) => void;

export function useSideElements(
  elements: IMessageElement[],
  setSideView: SetSideView
) {
  const knownSideElementsRef = useRef<Map<string, IMessageElement>>(new Map());
  const knownSideOrderRef = useRef<string[]>([]);

  useEffect(() => {
    const sideElements = elements.filter((e) => e.display === 'side');

    if (sideElements.length === 0) {
      knownSideElementsRef.current = new Map();
      knownSideOrderRef.current = [];
      setSideView(undefined);
      return;
    }

    const prevMap = knownSideElementsRef.current;
    const prevOrder = knownSideOrderRef.current;
    const currentIds = sideElements.map((e) => e.id);

    const hasChanged =
      currentIds.length !== prevOrder.length ||
      currentIds.some((id, i) => prevOrder[i] !== id) ||
      sideElements.some((e) => prevMap.get(e.id) !== e);

    if (!hasChanged) {
      return;
    }

    knownSideElementsRef.current = new Map(sideElements.map((e) => [e.id, e]));
    knownSideOrderRef.current = currentIds;

    // Keep a closed panel closed when every new or updated element opts out.
    // An already open panel still receives the update.
    const allOptedOut = sideElements.every(
      (e) => prevMap.get(e.id) === e || e.autoExpand === false
    );
    setSideView((current) =>
      current || !allOptedOut
        ? {
            title: sideElements[sideElements.length - 1].name,
            elements: sideElements
          }
        : current
    );
  }, [elements, setSideView]);
}
