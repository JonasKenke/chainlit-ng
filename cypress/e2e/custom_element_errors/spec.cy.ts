describe('Custom Element loader errors', () => {
  it('renders the error alert for a missing helper and a cycle', () => {
    cy.get('.step').should('have.length', 1);

    cy.get('.step').eq(0).find('.alert').should('have.length', 2);

    cy.get('.step')
      .eq(0)
      .find('.alert')
      .contains('Failed to load')
      .should('exist');

    cy.get('.step')
      .eq(0)
      .find('.alert')
      .contains('Circular import detected')
      .should('exist');

    cy.get('.step').eq(0).find('.inline-custom').should('not.exist');
  });
});
