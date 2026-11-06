const getRouterBasename = () => {
  const ogTitleMeta = document.querySelector('meta[property="og:root_path"]');
  if (ogTitleMeta && typeof ogTitleMeta.getAttribute('content') === 'string') {
    return ogTitleMeta.getAttribute('content')!;
  } else {
    return '';
  }
};

const ABSOLUTE_URL = /^[a-z][a-z0-9+.-]*:\/\//i;

// Absolute URL for an app route, keeping the configured root path.
export const getAppUrl = (path: string) => {
  if (ABSOLUTE_URL.test(path)) return path;
  const basename = getRouterBasename().replace(/\/+$/, '');
  return `${window.location.origin}${basename}/${path.replace(/^\/+/, '')}`;
};

export default getRouterBasename;
