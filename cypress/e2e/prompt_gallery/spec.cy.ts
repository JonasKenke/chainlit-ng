import { submitMessage } from '../../support/testUtils';

const login = () =>
  cy.request({
    method: 'POST',
    url: '/login',
    form: true,
    body: { username: 'alice', password: 'alice' }
  });

describe('Prompt Gallery', () => {
  beforeEach(() => {
    login();
    cy.visit('/');
  });

  it('shows gallery button in message composer', () => {
    cy.get('#prompt-gallery-open').should('exist');
  });

  it('opens a dialog (not a small popover) on gallery button click', () => {
    cy.get('#prompt-gallery-open').click();
    // Radix Dialog renders role="dialog"
    cy.get('[role="dialog"]').should('be.visible');
    // Dialog header contains the gallery title
    cy.get('[role="dialog"]').contains('Prompt Gallery').should('be.visible');
  });

  it('dialog contains a New Prompt button', () => {
    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').should('be.visible');
    cy.get('#prompt-gallery-new').should('be.visible');
  });

  it('dialog contains a search input', () => {
    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').should('be.visible');
    cy.get('[role="dialog"] input[placeholder="Search prompts..."]').should(
      'be.visible'
    );
  });

  it('creates a new prompt via New Prompt button', () => {
    // Open gallery, click New Prompt
    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').should('be.visible');
    cy.get('#prompt-gallery-new').click();

    // PromptSaveDialog should now be open
    cy.get('[role="dialog"]').should('be.visible');
    cy.get('#prompt-title').type('My E2E Prompt');
    cy.get('#prompt-content').type('Write a summary of: {text}', {
      parseSpecialCharSequences: false
    });
    cy.contains('button', 'Save').click();

    // The gallery remains open after the save dialog closes.
    cy.get('[role="dialog"]').should('be.visible');
    cy.contains('My E2E Prompt').should('be.visible');
  });

  it('inserts prompt content into composer on click', () => {
    // First create a prompt (build on previous state via ordering)
    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').should('be.visible');
    cy.get('#prompt-gallery-new').click();
    cy.get('#prompt-title').type('Insert Test');
    cy.get('#prompt-content').type('Hello from gallery');
    cy.contains('button', 'Save').click();

    // The gallery remains open after the save dialog closes.
    cy.contains('Insert Test').click();

    // Dialog should close and composer should have the prompt text
    cy.get('[role="dialog"]').should('not.exist');
    cy.get('#chat-input').should('have.value', 'Hello from gallery');
  });

  it('shows bookmark icon on user message hover', () => {
    // Send a message first
    submitMessage('Bookmark me please');
    cy.get('.step').should('have.length.gte', 1);

    // Hover the user message row to reveal bookmark button
    cy.get('.step').first().trigger('mouseover');
    cy.get('[aria-label="Save prompt"]').should('exist');
  });

  it('saves a user message as prompt via bookmark icon', () => {
    submitMessage('Prompt to bookmark');
    cy.get('.step').should('have.length.gte', 1);

    cy.get('.step').first().trigger('mouseover');
    cy.get('[aria-label="Save prompt"]').click({ force: true });

    // Save dialog opens pre-filled with message content
    cy.get('[role="dialog"]').should('be.visible');
    cy.get('#prompt-title').type('Bookmarked Prompt');
    cy.contains('button', 'Save').click();

    cy.get('[role="dialog"]').should('not.exist');
    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').should('be.visible');
    cy.contains('Bookmarked Prompt').should('be.visible');
  });

  it('can delete a prompt from the gallery', () => {
    // Create a prompt first
    cy.get('#prompt-gallery-open').click();
    cy.get('#prompt-gallery-new').click();
    cy.get('#prompt-title').type('To Delete');
    cy.get('#prompt-content').type('Delete me');
    cy.contains('button', 'Save').click();

    // The gallery remains open after the save dialog closes.
    cy.contains('To Delete').should('be.visible');
    cy.get('[aria-label="Delete"]').first().click({ force: true });
    cy.contains('To Delete').should('not.exist');
  });

  it('closing edit dialog returns to gallery, not the app', () => {
    // Create a prompt to edit
    cy.get('#prompt-gallery-open').click();
    cy.get('#prompt-gallery-new').click();
    cy.get('#prompt-title').type('Edit Me');
    cy.get('#prompt-content').type('Original content');
    cy.contains('button', 'Save').click();

    // The gallery remains open after the save dialog closes.
    cy.contains('Edit Me').should('be.visible');
    cy.get('[aria-label="Edit prompt"]').first().click({ force: true });

    // Edit dialog is open
    cy.get('[role="dialog"]').contains('Edit Me').should('be.visible');

    // Cancel the edit — gallery should reappear, not the bare app
    cy.contains('button', 'Cancel').click();
    cy.get('[role="dialog"]').contains('Prompt Gallery').should('be.visible');
  });

  it('add-via-shared-link navigates back to app after saving', () => {
    // Create a prompt and share it — intercept to capture share URL
    cy.get('#prompt-gallery-open').click();
    cy.get('#prompt-gallery-new').click();
    cy.get('#prompt-title').type('Shared Prompt');
    cy.get('#prompt-content').type('Shared content');
    cy.contains('button', 'Save').click();

    // The gallery remains open after the save dialog closes.
    // Intercept the share API call to grab the prompt ID
    cy.intercept('POST', '/project/prompts/*/share').as('shareReq');
    cy.contains('Shared Prompt').should('be.visible');
    // Click the copy/share icon (Copy icon button)
    cy.get('[aria-label="Share Prompt"]').first().click({ force: true });

    cy.wait('@shareReq').then((interception) => {
      const shareUrl: string = interception.response?.body?.shareUrl;
      expect(shareUrl).to.match(/^\/prompt\//);

      // Navigate to the share page as if opening the link
      cy.visit(shareUrl);
      cy.contains('Shared Prompt').should('be.visible');
      cy.contains('Shared content').should('be.visible');

      // Add to gallery
      cy.contains('button', 'Add to my gallery').click();

      // Should redirect back to the home page
      cy.url().should('eq', Cypress.config('baseUrl') + '/');
    });
  });

  it('can filter prompts by typing in search', () => {
    // Create two prompts
    cy.get('#prompt-gallery-open').click();
    cy.get('#prompt-gallery-new').click();
    cy.get('#prompt-title').type('Alpha Prompt');
    cy.get('#prompt-content').type('Alpha content');
    cy.contains('button', 'Save').click();

    cy.get('#prompt-gallery-new').click();
    cy.get('#prompt-title').type('Beta Prompt');
    cy.get('#prompt-content').type('Beta content');
    cy.contains('button', 'Save').click();

    // The gallery remains open after the save dialog closes.
    cy.get('[role="dialog"] input[placeholder="Search prompts..."]')
      .first()
      .type('Alpha');
    cy.contains('Alpha Prompt').should('be.visible');
    cy.contains('Beta Prompt').should('not.exist');
  });
});
