import { Suspense, lazy } from 'react';

import type { ICustomElement } from '@chainlit/react-client';

import { Skeleton } from '@/components/ui/skeleton';

interface Props {
  element: ICustomElement;
}
const CustomElement = lazy(() => import('@/components/Elements/CustomElement'));

const LazyCustomElement = ({ element }: Props) => {
  return (
    <Suspense fallback={<Skeleton className="h-20 rounded-md" />}>
      <CustomElement element={element} />
    </Suspense>
  );
};

export { LazyCustomElement };
