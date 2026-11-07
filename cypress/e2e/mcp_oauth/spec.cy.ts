const AUTH_URL = 'https://auth.example.test/authorize?state=abc123';

const pendingMcp = {
  name: 'stripe',
  tools: [],
  isUserProvided: true,
  clientType: 'streamable-http',
  url: 'https://mcp.example.test/mcp'
};

const openMcpDialog = () => {
  cy.visit('/');
  cy.get('#chat-input').should('exist');
  cy.get('.lucide-plug').closest('button').click();
  cy.get('#mcp-servers').should('be.visible');
};

const selectStreamableHttp = () => {
  cy.get('#server-type').click();
  cy.get('[role="option"]').contains('streamable-http').click();
  // Wait for the menu to unmount; typing into the form while it closes drops
  // keystrokes and leaves the Add button disabled.
  cy.get('[role="option"]').should('not.exist');
};

const fillUserServer = (name: string, url: string) => {
  cy.get('#server-name').type(name);
  cy.get('#server-name').should('have.value', name);
  cy.get('#server-url').type(url);
  cy.get('#server-url').should('have.value', url);
};

describe('MCP OAuth sign-in', () => {
  it('offers the OAuth opt-in only for streamable HTTP', () => {
    openMcpDialog();

    // The default transport is SSE and must not offer OAuth.
    cy.get('#server-oauth').should('not.exist');

    selectStreamableHttp();
    cy.get('#server-oauth').should('be.visible');

    cy.get('#server-type').click();
    cy.get('[role="option"]').contains('sse').click();
    cy.get('#server-oauth').should('not.exist');
  });

  it('keeps a 202 pending and opens the authorization URL on click', () => {
    cy.intercept('POST', '**/mcp', {
      statusCode: 202,
      body: { status: 'authorization_required', url: AUTH_URL, mcp: pendingMcp }
    }).as('connect');

    openMcpDialog();
    selectStreamableHttp();
    fillUserServer('stripe', 'https://mcp.example.test/mcp');
    cy.get('#server-oauth').click();
    cy.get('#mcp-servers').contains('button', 'Add').click();

    cy.wait('@connect').its('request.body').should('deep.include', {
      useOAuth: true,
      clientType: 'streamable-http'
    });

    cy.get('#mcp-servers').contains('h3', 'stripe').should('be.visible');
    cy.contains('Sign in to auth.example.test').should('be.visible');

    cy.window().then((win) => {
      cy.stub(win, 'open').as('open');
    });
    cy.contains('a', 'Sign in to auth.example.test').click();
    cy.get('@open').should(
      'have.been.calledWith',
      AUTH_URL,
      '_blank',
      'noopener,noreferrer'
    );
  });

  it('removes a pending sign-in and cancels it on the backend', () => {
    cy.intercept('POST', '**/mcp', {
      statusCode: 202,
      body: { status: 'authorization_required', url: AUTH_URL, mcp: pendingMcp }
    }).as('connect');
    cy.intercept('DELETE', '**/mcp', {
      statusCode: 200,
      body: { success: true }
    }).as('disconnect');

    openMcpDialog();
    selectStreamableHttp();
    fillUserServer('stripe', 'https://mcp.example.test/mcp');
    cy.get('#server-oauth').click();
    cy.get('#mcp-servers').contains('button', 'Add').click();
    cy.wait('@connect');

    cy.get('#mcp-servers')
      .contains('h3', 'stripe')
      .closest('.border')
      .within(() => {
        cy.get('button.text-destructive').click();
      });
    cy.get('[role="alertdialog"]').contains('button', 'Confirm').click();

    cy.wait('@disconnect')
      .its('request.body')
      .should('deep.include', { name: 'stripe' });
    cy.contains('h3', 'stripe').should('not.exist');
  });

  it('displays a backend 400 detail instead of a generic error', () => {
    const detail =
      'OAuth is not supported for SSE MCP connections; use streamable HTTP';
    cy.intercept('POST', '**/mcp', {
      statusCode: 400,
      body: { detail }
    }).as('connect');

    openMcpDialog();
    selectStreamableHttp();
    fillUserServer('stripe', 'https://mcp.example.test/mcp');
    cy.get('#server-oauth').click();
    cy.get('#mcp-servers').contains('button', 'Add').click();

    cy.wait('@connect');
    cy.get('[data-sonner-toast]', { timeout: 15000 }).should('contain', detail);
  });
});
