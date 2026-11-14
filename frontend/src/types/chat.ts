import { Socket } from '@chainlit-ng/react-client';

export interface IToken {
  id: number | string;
  token: string;
  isSequence: boolean;
}

export interface ISession {
  socket: Socket;
  error?: boolean;
}
