const getRouterBasename = () => {
  const ogTitleMeta = document.querySelector('meta[property="og:root_path"]');
  if (ogTitleMeta && typeof ogTitleMeta.getAttribute('content') === 'string') {
    return ogTitleMeta.getAttribute('content')!;
  } else {
    return '';
  }
};

// Prefix root-relative asset URLs with the runtime app root, leaving external URLs alone.
export const prefixAssetUrl = (url: string) => {
  if (!url.startsWith('/') || url.startsWith('//')) return url;
  return `${getRouterBasename().replace(/\/$/, '')}${url}`;
};

export default getRouterBasename;
