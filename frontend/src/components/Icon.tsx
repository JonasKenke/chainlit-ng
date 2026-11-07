import { Suspense, lazy } from 'react';

const IconDynamic = lazy(() => import('@/components/IconDynamic'));

interface Props {
  name: string;
  className?: string;
  size?: number;
  color?: string;
}

const Icon = ({ name, className, size, color }: Props) => {
  // Reserve the icon box while the registry or the per-icon chunk loads so
  // buttons and avatars do not shift once the icon arrives.
  const placeholder = (
    <svg
      className={className}
      width={size ?? 24}
      height={size ?? 24}
      aria-hidden="true"
      data-icon-placeholder
    />
  );

  return (
    <Suspense fallback={placeholder}>
      <IconDynamic
        name={name}
        className={className}
        size={size}
        color={color}
        placeholder={placeholder}
      />
    </Suspense>
  );
};

export default Icon;
