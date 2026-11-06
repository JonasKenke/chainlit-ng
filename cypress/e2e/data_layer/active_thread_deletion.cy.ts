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

describe('deleting the active thread', () => {
  before(cleanupThreadHistory);
  afterEach(cleanupThreadHistory);

  it('leaves the thread route before the deletion request and does not bring it back', () => {
    let releaseDeletion: (() => void) | undefined;

    cy.intercept('POST', '/project/threads').as('listThreads');
    cy.intercept('DELETE', '/project/thread', () => {
      return new Cypress.Promise<void>((resolve) => {
        releaseDeletion = resolve;
      });
    }).as('deleteThread');

    login();

    cy.get('#thread-test1').click();
    cy.location('pathname').should('eq', '/thread/test1');
    cy.get('.step').should('contain.text', 'Message 1');

    cy.get('#thread-test1').find('#thread-options').click();
    cy.get('#delete-thread').click();
    cy.get("[role='alertdialog'] button.bg-primary").click();

    // The deletion request is still held, so the route must have changed
    // before the deleted thread can be resumed again.
    cy.location('pathname').should('eq', '/');
    cy.wrap(null).should(() => expect(releaseDeletion).to.be.a('function'));
    cy.then(() => releaseDeletion?.());
    cy.wait('@deleteThread');

    cy.get('#thread-test1').should('not.exist');
    cy.get('#thread-history').should('contain.text', 'thread 2');
    cy.get('.step').should('contain.text', 'Hello, send me a message!');
    cy.get('[data-step-type="user_message"]').should('not.exist');

    cy.reload();
    cy.location('pathname').should('eq', '/');
    cy.get('#thread-history').should('contain.text', 'thread 2');
    cy.get('#thread-test1').should('not.exist');
  });
});
