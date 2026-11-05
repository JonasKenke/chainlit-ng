import { setupWebSocketListener } from '../../support/testUtils';

const submitMessage = (message: string) => {
  cy.get('#chat-input').type(message);
  // ponytail: force skips actionability; remove when Cypress handles this route-change click.
  cy.get('#chat-submit').should('not.be.disabled').click({ force: true });
};

const login = (user: 'alice' | 'bob') => {
  cy.visit('/');
  cy.location('pathname').should('eq', '/login');

  cy.get("input[name='email']").clear();
  cy.get("input[name='email']").type(user);
  cy.get("input[name='password']").clear();
  cy.get("input[name='password']").type(user === 'alice' ? 'a' : 'b');

  cy.intercept('POST', '/login').as('loginReq');
  cy.intercept('GET', '/user').as('userReq');

  cy.get("button[type='submit']").click();

  cy.location('pathname', { timeout: 10000 }).should('eq', '/');
};

describe('Thread resume (author)', () => {
  it('resumes own thread, composer visible, can continue chatting', () => {
    let resumed = false;
    setupWebSocketListener('resume_thread', () => {
      resumed = true;
    });

    login('alice');

    // Wait for the welcome composer to be replaced by the footer composer.
    // The remount resets the draft, so typing before it drops the message.
    cy.get('#message-composer').should('be.visible');
    cy.get('#welcome-screen').should('not.exist');

    // Start a thread
    submitMessage('hi');
    cy.location('pathname').should('match', /\/thread\//);

    // Reload to trigger resume
    cy.reload();

    // Composer present and no read-only banner
    cy.get('#message-composer').should('be.visible');
    cy.get('[data-testid="read-only-banner"]').should('not.exist');

    // Wait for resume_thread to be applied before typing.
    cy.wrap(null).should(() => expect(resumed).to.be.true);

    // Continue chatting
    submitMessage('still here');
    cy.get("[data-step-type='assistant_message']").contains('Echo: still here');
  });
});
