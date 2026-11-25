import type { Element, Root } from 'hast';
import { visit } from 'unist-util-visit';

/**
 * Active-content allowlist for raw Markdown HTML.
 *
 * `unsafe_allow_html` opts into HTML formatting, not into executable
 * documents. Active containers are removed rather than merely cleaned of
 * attributes. Raw style elements can import remote CSS or apply global rules
 * to the embedding document, so they are blocked while style attributes remain.
 */
const BLOCKED_TAGS: Record<string, true> = {
  base: true,
  button: true,
  embed: true,
  form: true,
  iframe: true,
  input: true,
  link: true,
  meta: true,
  object: true,
  option: true,
  script: true,
  select: true,
  style: true,
  textarea: true
};

const URL_ATTRIBUTES: Record<string, true> = {
  action: true,
  data: true,
  formaction: true,
  href: true,
  poster: true,
  src: true,
  'xlink:href': true
};

function isExecutableUrl(value: unknown): boolean {
  if (typeof value !== 'string') return false;
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[\p{Cc}\s]+/gu, '');
  return (
    normalized.startsWith('javascript:') ||
    normalized.startsWith('vbscript:') ||
    normalized.startsWith('data:text/html')
  );
}

export function sanitizeRawHtml() {
  return (tree: Root) => {
    visit(tree, 'element', (node: Element, index, parent) => {
      if (!parent || index === undefined) return;

      if (Object.hasOwn(BLOCKED_TAGS, node.tagName)) {
        parent.children.splice(index, 1);
        return index;
      }

      const properties = node.properties;
      if (!properties) return;

      for (const key of Object.keys(properties)) {
        const normalizedKey = key.toLowerCase();
        if (normalizedKey.startsWith('on') || normalizedKey === 'srcdoc') {
          delete properties[key];
          continue;
        }
        if (
          Object.hasOwn(URL_ATTRIBUTES, normalizedKey) &&
          isExecutableUrl(properties[key])
        ) {
          delete properties[key];
        }
      }

      if (node.tagName === 'a' && properties.target === '_blank') {
        properties.rel = ['noopener', 'noreferrer'];
      }
    });
  };
}
