// Import the toBeInTheDocument function
import type { ITextElement } from '@chainlit-ng/react-client';
import { render } from '@testing-library/react';
import { expect, it } from 'vitest';

import { MessageContent } from 'components/chat/Messages/Message/Content';

it('renders the message content', () => {
  const { getByText } = render(
    <MessageContent
      message={{
        threadId: 'test',
        type: 'assistant_message',
        output: 'Hello World',
        id: 'test',
        name: 'User',
        createdAt: 0
      }}
      elements={[]}
    />
  );
  expect(getByText('Hello World')).toBeInTheDocument();
});

it('highlights multiple sources correctly (no substring matching)', () => {
  const { getByRole } = render(
    <MessageContent
      message={{
        threadId: 'test',
        type: 'assistant_message',
        output: `Hello world source_121, source_1, source_12`,
        id: 'test2',
        name: 'Test',
        createdAt: 0
      }}
      elements={[
        {
          name: 'source_1',
          type: 'text',
          display: 'side',
          url: 'source_1',
          forId: 'test2'
        } as ITextElement,
        {
          name: 'source_12',
          display: 'side',
          type: 'text',
          forId: 'test2',
          url: 'hi'
        } as ITextElement,
        {
          name: 'source_121',
          display: 'side',
          type: 'text',
          forId: 'test2',
          url: 'hi'
        } as ITextElement
      ]}
    />
  );
  expect(getByRole('link', { name: 'source_1' })).toBeInTheDocument();
  expect(getByRole('link', { name: 'source_12' })).toBeInTheDocument();
  expect(getByRole('link', { name: 'source_121' })).toBeInTheDocument();
});

it('highlights sources containing regex characters correctly', () => {
  const { getByRole } = render(
    <MessageContent
      message={{
        threadId: 'test',
        type: 'assistant_message',
        output: `Hello world: Document[1], source(12), page{12}`,
        id: 'test2',
        name: 'Test',
        createdAt: 0
      }}
      elements={[
        {
          name: 'Document[1]',
          display: 'side',
          type: 'text',
          url: 'hi',
          forId: 'test2'
        } as ITextElement,
        {
          name: 'source(12)',
          display: 'side',
          type: 'text',
          url: 'hi',
          forId: 'test2'
        } as ITextElement,
        {
          name: 'page{12}',
          display: 'side',
          type: 'text',
          url: 'hi',
          forId: 'test2'
        } as ITextElement
      ]}
    />
  );
  expect(getByRole('link', { name: 'Document[1]' })).toBeInTheDocument();
  expect(getByRole('link', { name: 'source(12)' })).toBeInTheDocument();
  expect(getByRole('link', { name: 'page{12}' })).toBeInTheDocument();
});

it('preserves natural-language colons parsed as text directives', () => {
  const content = '💬 TV:n är stor; FN:s huvudkontor; foo:bar';
  const { container } = render(
    <MessageContent
      message={{
        threadId: 'test',
        type: 'assistant_message',
        output: content,
        id: 'text-directive',
        name: 'Assistant',
        createdAt: 0
      }}
      elements={[]}
    />
  );

  expect(container.textContent).toBe(content);
});

it('preserves the complete syntax of unhandled text directives', () => {
  const content =
    'A :abbr[lovely language]{title="HyperText Markup Language"}.';
  const { container } = render(
    <MessageContent
      message={{
        threadId: 'test',
        type: 'assistant_message',
        output: content,
        id: 'unhandled-text-directive',
        name: 'Assistant',
        createdAt: 0
      }}
      elements={[]}
    />
  );

  expect(container.textContent).toBe(content);
});

it('keeps container directive content rendered', () => {
  const { container } = render(
    <MessageContent
      message={{
        threadId: 'test',
        type: 'assistant_message',
        output: ':::info\nHello alert\n:::',
        id: 'container-directive',
        name: 'Assistant',
        createdAt: 0
      }}
      elements={[]}
    />
  );

  expect(container.textContent).toBe('Hello alert');
});

const inlineTextElement = (forId: string): ITextElement => ({
  id: `${forId}-element`,
  name: 'Inline Element',
  type: 'text',
  display: 'inline',
  url: 'inline-element.txt',
  forId
});

const renderWithElementsPosition = (elementsPosition?: string) => {
  const id = `elements-position-${elementsPosition ?? 'unset'}`;
  const result = render(
    <MessageContent
      message={{
        threadId: 'test',
        type: 'assistant_message',
        output: 'Hello World',
        id,
        name: 'Assistant',
        createdAt: 0,
        metadata:
          elementsPosition === undefined ? undefined : { elementsPosition }
      }}
      elements={[inlineTextElement(id)]}
    />
  );

  const content = result.container.querySelector('.message-content');
  const text = content?.textContent ?? '';
  return {
    textIndex: text.indexOf('Hello World'),
    elementIndex: text.indexOf('Inline Element')
  };
};

it('renders inline elements below the content by default', () => {
  const { textIndex, elementIndex } = renderWithElementsPosition();

  expect(textIndex).toBeGreaterThanOrEqual(0);
  expect(elementIndex).toBeGreaterThan(textIndex);
});

it('renders inline elements below the content for elementsPosition "below"', () => {
  const { textIndex, elementIndex } = renderWithElementsPosition('below');

  expect(textIndex).toBeGreaterThanOrEqual(0);
  expect(elementIndex).toBeGreaterThan(textIndex);
});

it('renders inline elements above the content for elementsPosition "above"', () => {
  const { textIndex, elementIndex } = renderWithElementsPosition('above');

  expect(textIndex).toBeGreaterThanOrEqual(0);
  expect(elementIndex).toBeGreaterThanOrEqual(0);
  expect(elementIndex).toBeLessThan(textIndex);
});

it('treats an unknown elementsPosition as below', () => {
  const { textIndex, elementIndex } = renderWithElementsPosition('sideways');

  expect(textIndex).toBeGreaterThanOrEqual(0);
  expect(elementIndex).toBeGreaterThan(textIndex);
});
