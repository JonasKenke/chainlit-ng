import { setupWebSocketListener, submitMessage } from '../../support/testUtils';

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
    setupWebSocketListener('approval', () => {
      approvalEvents += 1;
    });
    login();
    cy.visit('/');

    submitMessage('ask');
    cy.get('[data-testid="approval-card"]').should('have.length', 1);
    cy.get('[data-testid="approval-card"]').should('contain', 'send_email');

    cy.request('/test/disconnect');
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
