import type { IPdfElement } from '@chainlit-ng/react-client';
import { Suspense, lazy } from 'react';

import { Skeleton } from '@/components/ui/skeleton';

interface Props {
  element: IPdfElement;
}
const PdfElement = lazy(() =>
  import('@/components/Elements/PDF').then((m) => ({ default: m.PDFElement }))
);

const LazyPDF = ({ element }: Props) => {
  return (
    <Suspense
      fallback={<Skeleton className="h-full min-h-[160px] rounded-md" />}
    >
      <PdfElement element={element} />
    </Suspense>
  );
};

export { LazyPDF };
