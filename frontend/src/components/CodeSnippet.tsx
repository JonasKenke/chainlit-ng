import { cn } from '@/lib/utils';
import { useEffect, useRef } from 'react';

import { Card, CardContent, CardHeader } from '@/components/ui/card';

import 'highlight.js/styles/monokai-sublime.css';

import CopyButton from './CopyButton';

interface CodeSnippetProps {
  language: string;
  children: string;
}

const HighlightedCode = ({ language, children }: CodeSnippetProps) => {
  const codeRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let cancelled = false;

    import('highlight.js')
      .then(({ default: hljs }) => {
        const code = codeRef.current;
        // Ignore a load that finishes after an unmount or newer content.
        if (cancelled || !code || code.textContent !== children) {
          return;
        }

        if (!hljs.getLanguage(language)) {
          code.classList.replace(`language-${language}`, 'language-txt');
        }

        code.removeAttribute('data-highlighted');
        hljs.highlightElement(code);
      })
      .catch(() => {
        // Keep the plain code block if the chunk fails to load.
      });

    return () => {
      cancelled = true;
    };
  }, [language, children]);

  return (
    <pre className="m-0">
      <code
        ref={codeRef}
        className={`language-${language} hljs font-mono text-sm rounded-b-md block`}
      >
        {children}
      </code>
    </pre>
  );
};

interface CodeProps {
  children: React.ReactNode;
  node?: {
    children?: Array<{
      properties?: {
        className?: string[];
      };
      children?: Array<{
        value?: string;
      }>;
    }>;
  };
}

export default function CodeSnippet({ ...props }: CodeProps) {
  const codeChildren = props.node?.children?.[0];
  const className = codeChildren?.properties?.className?.[0];
  const match = /language-(\w+)/.exec(className || '');
  const code = codeChildren?.children?.[0]?.value;

  const showSyntaxHighlighter = match && code;

  const highlightedCode = showSyntaxHighlighter ? (
    <HighlightedCode language={match[1]}>{code}</HighlightedCode>
  ) : null;

  const nonHighlightedCode = showSyntaxHighlighter ? null : (
    <div
      className={cn('rounded-b-md overflow-x-auto bg-accent', code && 'p-2')}
    >
      <code className="whitespace-pre-wrap">{code}</code>
    </div>
  );

  return (
    <Card className="relative my-2">
      <CardHeader className="flex flex-row items-center justify-between py-1 px-4">
        <span className="text-sm text-muted-foreground">
          {match?.[1] || 'Raw code'}
        </span>
        <CopyButton content={code} />
      </CardHeader>
      <CardContent className="p-0">
        {highlightedCode}
        {nonHighlightedCode}
      </CardContent>
    </Card>
  );
}
