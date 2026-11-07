import { sep } from 'path';

const cleanupThreadHistory = () => {
  const pathItems = Cypress.spec.absolute.split(sep);
  pathItems[pathItems.length - 1] = 'thread_history.pickle';
  cy.task('deleteFileIfExists', pathItems.join(sep));
};

const consoleErrors: string[] = [];

Cypress.on('window:before:load', (win) => {
  cy.stub(win.console, 'error').callsFake((...args: unknown[]) => {
    consoleErrors.push(args.map(String).join(' '));
  });
});

const waitForThreadList = () => {
  cy.wait('@listThreads').its('response.statusCode').should('eq', 200);
};

const login = (username: string) => {
  cy.location('pathname').should('eq', '/login');
  cy.get('#email').should('be.visible').type(username);
  cy.get('#password').type(`${username}{enter}`);
  cy.location('pathname').should('eq', '/');
  waitForThreadList();
};

const logout = () => {
  cy.get('#user-nav-button').click();
  cy.contains("[role='menuitem']", 'Logout').click();
  cy.location('pathname').should('eq', '/login');
};

const waitForChat = () => {
  cy.get('#chat-input').should('be.visible').and('not.be.disabled');
  cy.get('[data-step-type="assistant_message"]').should(
    'contain.text',
    'Hello, send me a message!'
  );
};

const createThread = (message: string) => {
  waitForChat();
  cy.get('#chat-input')
    .should('be.visible')
    .and('not.be.disabled')
    .type(message);
  cy.get('#chat-submit').should('not.be.disabled').click();
  cy.get('[data-step-type="assistant_message"]').should('contain.text', 'Ok!');
  cy.get('#thread-history').should('contain.text', message);
};

const startNewChat = () => {
  cy.get('#new-chat-button').first().click({ force: true });
  cy.get('#confirm').should('be.visible').click();
  cy.location('pathname').should('eq', '/');
};

const clearAllChats = () => {
  cy.get('#user-nav-button').click();
  cy.contains("[role='menuitem']", 'Clear all chats').click();

  cy.get("[role='alertdialog']")
    .should('be.visible')
    .and('contain.text', 'Delete all chats?')
    .and(
      'contain.text',
      'This permanently deletes all your chats, including their messages, elements, uploaded files, and shared links.'
    );

  cy.contains("[role='alertdialog'] button", 'Delete all chats').click();
  cy.wait('@clearAllThreads').its('response.statusCode').should('eq', 200);
};

describe('clearing all chat history', () => {
  before(cleanupThreadHistory);
  afterEach(cleanupThreadHistory);

  it('deletes only the current user threads and keeps them gone after reload', () => {
    cy.intercept('POST', '/project/threads').as('listThreads');
    cy.intercept('DELETE', '/project/threads').as('clearAllThreads');

    cy.step('User 2 starts two threads');
    login('user2');
    createThread('user2 first thread');
    startNewChat();
    createThread('user2 second thread');
    cy.get('#thread-history a[href^="/thread/"]').should('have.length', 2);

    logout();

    cy.step('User 1 sees their own threads');
    login('user1');
    cy.get('#thread-test1').should('contain.text', 'thread 1');
    cy.get('#thread-test2').should('contain.text', 'thread 2');

    cy.step('Share one of user 1 threads');
    cy.request('PUT', '/project/thread/share', {
      threadId: 'test1',
      isShared: true
    })
      .its('status')
      .should('eq', 200);
    cy.request('GET', '/project/share/test1').its('status').should('eq', 200);

    cy.step('Clear all chats');
    // Logout/login switches invalidate the previous JWT and abort in-flight
    // fetches, which the react-client logs as expected 401s. Only assert on the
    // errors produced by the clear-all flow itself.
    const clearWindowStart = consoleErrors.length;
    clearAllChats();
    cy.get('#thread-test1').should('not.exist');
    cy.get('#thread-test2').should('not.exist');
    cy.get('#thread-history').should('contain.text', 'No threads found');

    cy.step('Deleted threads stay deleted after reload');
    cy.reload();
    waitForThreadList();
    cy.get('#thread-test1').should('not.exist');
    cy.get('#thread-test2').should('not.exist');
    cy.get('#thread-history').should('contain.text', 'No threads found');

    cy.step('Shared link of a deleted thread returns 404');
    cy.request({ url: '/project/share/test1', failOnStatusCode: false })
      .its('status')
      .should('eq', 404);

    cy.step('User 2 threads are untouched');
    logout();
    login('user2');
    cy.get('#thread-history').should('contain.text', 'user2 first thread');
    cy.get('#thread-history').should('contain.text', 'user2 second thread');
    cy.get('#thread-history a[href^="/thread/"]').should('have.length', 2);

    cy.step('No console errors during the clear-all flow');
    cy.wrap(consoleErrors.slice(clearWindowStart)).should('deep.equal', []);
  });
});
