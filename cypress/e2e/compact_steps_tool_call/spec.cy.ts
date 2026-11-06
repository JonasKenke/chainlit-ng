import { submitMessage } from '../../support/testUtils';

describe('Compact CoT display with cot = "tool_call"', () => {
  it('counts only tools and keeps non-tool rendering unchanged', () => {
    submitMessage('Hello');

    cy.get('[data-testid="compact-steps"]').should('have.length', 1);
    cy.get('[data-testid="compact-steps-trigger"]')
      .should('contain', 'Used 2 tools')
      .and('have.attr', 'aria-expanded', 'false');

    // The non-tool wrapper is not counted and is skipped on expansion, while
    // its tool children render as in list mode.
    cy.get('[data-testid="compact-steps-trigger"]').click();
    cy.get('#step-tool1').should('be.visible');
    cy.get('#step-tool2').should('be.visible');
    cy.get('#step-wrapper').should('not.exist');

    cy.contains('Final answer').should('be.visible');
  });
});
