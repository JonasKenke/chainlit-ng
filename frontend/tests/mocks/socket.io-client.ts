import { vi } from 'vitest';

export const sockets: any[] = [];

export const io = vi.fn((_uri: string, options: any) => {
  const socket = {
    auth: options.auth,
    on: vi.fn(),
    emit: vi.fn(),
    removeAllListeners: vi.fn(),
    close: vi.fn(),
    disconnect: vi.fn()
  };
  sockets.push(socket);
  return socket;
});

export class Socket {}

export default io;
