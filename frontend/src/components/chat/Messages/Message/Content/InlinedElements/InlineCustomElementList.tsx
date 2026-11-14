import type { ICustomElement } from '@chainlit-ng/react-client';

import { LazyCustomElement } from '@/components/Elements/LazyCustomElement';

interface Props {
  items: ICustomElement[];
}

const InlinedCustomElementList = ({ items }: Props) => (
  <div className="flex flex-col gap-2">
    {items.map((customElement) => {
      return (
        <LazyCustomElement key={customElement.id} element={customElement} />
      );
    })}
  </div>
);

export { InlinedCustomElementList };
