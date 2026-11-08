import { submitMessage } from '../../support/testUtils';

const SOCKET_IO_EVENT_PREFIX = '42';
const SOCKET_IO_PREFIX_LENGTH = 2;

const login = () => {
  cy.intercept('/auth/header', (req) => {
    req.headers['test-header'] = 'approvals';
    req.reply();
  }).as('auth');
};

const waitForReconnect = (minimum: number) => {
  cy.window().then(async (win) => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const response = await win.fetch('/test/connections');
      const body = await response.json();
      if (body.count >= minimum) return;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    throw new Error('socket did not reconnect');
  });
};

describe('Approvals', () => {
  it('keeps one card across a socket reconnect and decides once', () => {
    let approvalEvents = 0;
    const sockets: WebSocket[] = [];

    // One stub for both jobs: a second WebSocket stub would shadow this one.
    cy.on('window:before:load', (win) => {
      const OriginalWebSocket = win.WebSocket;

      cy.stub(win, 'WebSocket').callsFake(
        (url: string, protocols?: string | string[]) => {
          const ws = new OriginalWebSocket(url, protocols);
          sockets.push(ws);

          ws.addEventListener('message', (event: MessageEvent) => {
            const data = event.data;
            if (
              typeof data === 'string' &&
              data.startsWith(SOCKET_IO_EVENT_PREFIX)
            ) {
              try {
                const payload = JSON.parse(data.slice(SOCKET_IO_PREFIX_LENGTH));
                if (payload[0] === 'approval') {
                  approvalEvents += 1;
                }
              } catch (_e) {
                // Ignore non-JSON frames.
              }
            }
          });

          return ws;
        }
      );
    });

    login();
    cy.visit('/');

    submitMessage('ask');
    cy.get('[data-testid="approval-card"]').should('have.length', 1);
    cy.get('[data-testid="approval-card"]').should('contain', 'send_email');
    cy.then(() => expect(sockets.length).to.be.greaterThan(0));

    // Close the transport from the page: socket.io reconnects and the server
    // runs connection_successful again. A server-side disconnect would either
    // disable auto-reconnect or be ignored by the client.
    cy.then(() => sockets.at(-1)!.close());
    waitForReconnect(2);

    // The server never re-emits a card; the client keeps the live one.
    cy.get('[data-testid="approval-card"]').should('have.length', 1);
    cy.then(() => expect(approvalEvents).to.equal(1));

    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Approve')
      .click();
    cy.get('[data-testid="approval-card"]').should('contain', 'Approved');
    cy.contains('executed');
  });
});
