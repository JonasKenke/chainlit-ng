import { sep } from 'path';

import { submitMessage } from '../../support/testUtils';

const cleanupThreadHistory = () => {
  const pathItems = Cypress.spec.absolute.split(sep);
  pathItems[pathItems.length - 1] = 'thread_history.pickle';
  cy.task('deleteFileIfExists', pathItems.join(sep));
};

describe('new chat during thread-history refresh', () => {
  afterEach(cleanupThreadHistory);

  const login = () => {
    cy.visit('/');
    cy.location('pathname').should('eq', '/login');
    cy.get('#email').type('user1');
    cy.get('#password').type('user1{enter}');
    cy.location('pathname').should('eq', '/');
    cy.wait('@listThreads');
    cy.get('.step').should('have.length', 2);
  };

  it('keeps the new chat active when the previous thread title refresh completes', () => {
    const title = `Previous conversation ${Date.now()}`;
    let historyRefreshStarted = false;
    let delayedThreadId: string | undefined;

    cy.intercept('POST', '/project/threads', (request) => {
      request.continue((response) => {
        const delayedThread = response.body.data.find(
          (thread: { id: string; name: string }) => thread.name === title
        );
        if (delayedThread) {
          historyRefreshStarted = true;
          delayedThreadId = delayedThread.id;
          response.setDelay(3000);
        }
      });
    }).as('listThreads');

    login();
    submitMessage(title);
    cy.get('[data-step-type="user_message"]').should('contain.text', title);
    cy.wrap(null).should(() => expect(historyRefreshStarted).to.equal(true));

    cy.get('#new-chat-button').first().click({ force: true });
    cy.get('#confirm').should('be.visible').click();
    cy.location('pathname').should('eq', '/');

    cy.wait('@listThreads');
    cy.get('#thread-history').should('contain.text', title);
    cy.get('[data-step-type="user_message"]').should('not.exist');
    cy.location('pathname').should('eq', '/');

    submitMessage('Continue in the new chat');
    cy.get('[data-step-type="user_message"]').should(
      'contain.text',
      'Continue in the new chat'
    );
    cy.location('pathname').should('match', /^\/thread\//);
    cy.location('pathname').should('not.eq', `/thread/${delayedThreadId}`);
  });

  for (const transport of ['websocket', 'polling']) {
    it(`starts a fresh chat after leaving a resumed thread over ${transport}`, () => {
      cy.intercept('POST', '/project/threads').as('listThreads');
      cy.on('window:before:load', (win) => {
        Object.assign(win, { transports: [transport] });
      });
      login();

      cy.get('#thread-test1').click();
      cy.location('pathname').should('eq', '/thread/test1');
      cy.get('.step').should('contain.text', 'Message 1');

      cy.get('#new-chat-button').first().click({ force: true });
      cy.get('#confirm').should('be.visible').click();

      cy.get('.step').should('contain.text', 'Hello, send me a message!');
      cy.get('.step').should('contain.text', 'Create step counter: 0');
      cy.get('[data-step-type="user_message"]').should('not.exist');
      cy.location('pathname').should('eq', '/');

      submitMessage('Continue after resuming');
      cy.get('[data-step-type="user_message"]').should(
        'contain.text',
        'Continue after resuming'
      );
      cy.location('pathname').should('match', /^\/thread\//);
      cy.location('pathname').should('not.eq', '/thread/test1');
      cy.get('.step').should('not.contain.text', 'Message 1');
    });
  }

  it('keeps the new chat active if the history response resolves with the confirm click', () => {
    const title = `Same-tick conversation ${Date.now()}`;
    let refreshStarted = false;
    let refreshReleased = false;
    let releaseResponse: (() => void) | undefined;

    cy.intercept('POST', '/project/threads', (request) => {
      request.continue((response) => {
        const hasTargetThread = response.body.data.some(
          (thread: { name: string }) => thread.name === title
        );
        if (!hasTargetThread) return;

        refreshStarted = true;
        return new Cypress.Promise<void>((resolve) => {
          releaseResponse = () => {
            refreshReleased = true;
            resolve();
          };
        });
      });
    }).as('listThreads');

    login();
    submitMessage(title);
    cy.get('[data-step-type="user_message"]').should('contain.text', title);
    cy.wrap(null).should(() => expect(refreshStarted).to.equal(true));

    cy.get('#new-chat-button').first().click({ force: true });
    cy.get('#confirm').should('be.visible');
    cy.get('#confirm').then(($confirm) => {
      $confirm[0].addEventListener('click', () => releaseResponse?.(), {
        capture: true,
        once: true
      });
    });
    cy.get('#confirm').click();

    cy.wrap(null).should(() => expect(refreshReleased).to.equal(true));
    cy.wait('@listThreads');
    cy.get('#thread-history').should('contain.text', title);
    cy.get('[data-step-type="user_message"]').should('not.exist');
    cy.location('pathname').should('eq', '/');
  });
});
