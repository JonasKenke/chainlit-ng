import type { IPdfElement } from '@chainlit/react-client';

import { LazyPDF } from '@/components/Elements/LazyPDF';

interface Props {
  items: IPdfElement[];
}

const InlinedPDFList = ({ items }: Props) => (
  <div className="flex flex-col gap-2">
    {items.map((pdf, i) => {
      return (
        <div key={i}>
          <LazyPDF element={pdf} />
        </div>
      );
    })}
  </div>
);

export { InlinedPDFList };
