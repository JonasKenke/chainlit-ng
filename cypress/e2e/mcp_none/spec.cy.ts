describe('MCP button gating', () => {
  it('hides the MCP button when no server is configured', () => {
    cy.visit('/');
    cy.get('#chat-input').should('exist');
    cy.get('.lucide-plug').should('not.exist');
  });
});
