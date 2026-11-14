import { IAction } from '@chainlit-ng/react-client';

import { ActionButton } from './ActionButton';

interface Props {
  actions: IAction[];
}

export default function MessageActions({ actions }: Props) {
  return (
    <>
      {actions.map((a) => (
        <ActionButton action={a} key={a.id} />
      ))}
    </>
  );
}
