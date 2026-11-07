import { Suspense, lazy } from 'react';

import type { IPdfElement } from '@chainlit/react-client';

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
