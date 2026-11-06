import { sep } from 'path';

const cleanupThreadHistory = () => {
  const pathItems = Cypress.spec.absolute.split(sep);
  pathItems[pathItems.length - 1] = 'thread_history.pickle';
  cy.task('deleteFileIfExists', pathItems.join(sep));
};

const login = () => {
  cy.location('pathname').should('eq', '/login');
  cy.get('#email').type('user1');
  cy.get('#password').type('user1{enter}');
  cy.location('pathname').should('eq', '/');
  cy.wait('@listThreads');
  cy.get('.step').should('have.length', 2);
};

const deleteActiveThread = () => {
  cy.get('#thread-test1').find('#thread-options').click();
  cy.get('#delete-thread').click();
  cy.get("[role='alertdialog'] button.bg-primary").click();
};

describe('failed deletion of the active thread', () => {
  before(cleanupThreadHistory);
  afterEach(cleanupThreadHistory);

  it('restores the resumed chat and cleans up on retry', () => {
    let failDeletion = true;

    cy.intercept('POST', '/project/threads').as('listThreads');
    cy.intercept('DELETE', '/project/thread', (req) => {
      if (failDeletion) {
        failDeletion = false;
        req.reply({
          statusCode: 500,
          body: { detail: 'Internal Server Error' }
        });
      } else {
        req.continue();
      }
    }).as('deleteThread');

    login();

    cy.get('#thread-test1').click();
    cy.location('pathname').should('eq', '/thread/test1');
    cy.get('.step').should('contain.text', 'Message 1');

    deleteActiveThread();
    cy.wait('@deleteThread');

    // The deletion failed, so the thread must be back with a live session:
    // #chat-input only renders when the resume completed.
    cy.get('[data-sonner-toast]').should(
      'contain.text',
      'Internal Server Error'
    );
    cy.location('pathname').should('eq', '/thread/test1');
    cy.get('#chat-input').should('exist');
    cy.get('.step').should('contain.text', 'Message 1');
    cy.get('#thread-test1').should('exist');

    // A retry that succeeds cleans up normally.
    deleteActiveThread();
    cy.wait('@deleteThread');
    cy.location('pathname').should('eq', '/');
    cy.get('#thread-test1').should('not.exist');
    cy.get('.step').should('contain.text', 'Hello, send me a message!');
  });
});
