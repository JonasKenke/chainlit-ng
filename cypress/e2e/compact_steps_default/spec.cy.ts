import { submitMessage } from '../../support/testUtils';

describe('Default CoT display (cot_display omitted)', () => {
  it('renders each step as before, with no compact summary', () => {
    submitMessage('Hello');

    cy.get('[data-testid="compact-steps"]').should('not.exist');

    cy.get('#step-tool1').should('be.visible');
    cy.get('#step-tool2').should('be.visible');
    cy.get('#step-tool3').should('be.visible');

    cy.contains('Final answer').should('be.visible');

    // User message, three steps and the assistant message, as list mode renders.
    cy.get('.step').should('have.length', 5);
  });
});
