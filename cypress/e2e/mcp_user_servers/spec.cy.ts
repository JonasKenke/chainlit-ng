describe('MCP user-provided servers', () => {
  it('adds an allowlisted user server', () => {
    cy.intercept('POST', '**/mcp', {
      statusCode: 200,
      body: {
        success: true,
        mcp: {
          name: 'user-server',
          tools: [{ name: 'ping' }],
          isUserProvided: true,
          clientType: 'sse',
          url: 'http://127.0.0.1:9/mcp'
        }
      }
    }).as('connectUser');

    cy.visit('/');
    cy.get('#chat-input').should('exist');
    cy.get('.lucide-plug').should('be.visible');
    cy.get('.lucide-plug').closest('button').click();
    cy.get('#mcp-servers').should('be.visible');

    cy.contains('Add your own server').should('be.visible');
    cy.get('#server-name').type('user-server');
    cy.get('#server-url').type('http://127.0.0.1:9/mcp');
    cy.get('#mcp-servers').contains('button', 'Add').click();

    cy.wait('@connectUser')
      .its('request.body')
      .should((body) => {
        expect(body).to.deep.include({
          name: 'user-server',
          clientType: 'sse',
          url: 'http://127.0.0.1:9/mcp'
        });
        expect(body).to.not.have.property('command');
      });

    cy.get('#mcp-servers').contains('h3', 'user-server').should('be.visible');
    cy.get('#mcp-servers')
      .contains('h3', 'user-server')
      .closest('div.border')
      .within(() => {
        cy.contains('ping').should('exist');
      });
  });

  it('shows the config error for a URL outside the allowlist', () => {
    cy.visit('/');
    cy.get('#chat-input').should('exist');
    cy.get('.lucide-plug').should('be.visible');
    cy.get('.lucide-plug').closest('button').click();
    cy.get('#mcp-servers').should('be.visible');

    cy.get('#server-name').type('not-allowed');
    cy.get('#server-url').type('https://evil.example.test/mcp');
    cy.get('#mcp-servers').contains('button', 'Add').click();

    // No stub: the backend allowlist rejects before any MCP connection opens.
    cy.get('[data-sonner-toast]', { timeout: 15000 }).should(
      'contain',
      'is not in the allowed MCP URL list'
    );
    cy.get('#mcp-servers').contains('h3', 'not-allowed').should('not.exist');
  });
});
