import { submitMessage } from '../../support/testUtils';

describe('Compact CoT display', () => {
  it('collapses multiple steps into a single collapsed summary', () => {
    submitMessage('multi');

    cy.get('[data-testid="compact-steps"]').should('have.length', 1);
    cy.get('[data-testid="compact-steps-trigger"]')
      .should('contain', 'Used 3 steps')
      .and('have.attr', 'aria-expanded', 'false');

    // The final answer renders at the root, outside the collapsed summary.
    cy.contains('Final answer').should('be.visible');

    // Individual steps stay hidden until the summary is expanded.
    cy.get('#step-tool1').should('not.exist');

    cy.get('[data-testid="compact-steps-trigger"]').click();
    cy.get('[data-testid="compact-steps-trigger"]').should(
      'have.attr',
      'aria-expanded',
      'true'
    );
    cy.get('#step-tool1').should('be.visible');
    cy.get('#step-tool2').should('be.visible');
    cy.get('#step-tool3').should('be.visible');
  });

  it('uses a native button and keeps aria state in sync', () => {
    submitMessage('multi');

    // Radix renders a native button, so Enter/Space activation is handled by
    // the browser. Cypress cannot synthesise that default action, so the test
    // verifies button semantics and the ARIA state toggled by activation.
    cy.get('[data-testid="compact-steps-trigger"]')
      .should('have.prop', 'tagName', 'BUTTON')
      .and('have.attr', 'aria-expanded', 'false')
      .and('not.have.attr', 'aria-controls');

    cy.get('[data-testid="compact-steps-trigger"]').focus();
    cy.focused().should('have.attr', 'data-testid', 'compact-steps-trigger');

    cy.get('[data-testid="compact-steps-trigger"]').click();
    cy.get('[data-testid="compact-steps-trigger"]').should(
      'have.attr',
      'aria-expanded',
      'true'
    );
    cy.get('[data-testid="compact-steps-trigger"]')
      .invoke('attr', 'aria-controls')
      .should('not.be.empty')
      .then((controls) => {
        cy.get(`[id="${controls}"]`).should('exist');
      });
  });

  it('does not collapse a single step', () => {
    submitMessage('single');

    cy.get('#step-tool1').should('be.visible');
    cy.get('[data-testid="compact-steps"]').should('not.exist');
    cy.contains('Final answer').should('be.visible');
  });

  it('counts nested steps recursively to trigger compact mode', () => {
    submitMessage('nested');

    cy.get('[data-testid="compact-steps"]').should('have.length', 1);
    cy.get('[data-testid="compact-steps-trigger"]').should(
      'contain',
      'Used 3 steps'
    );

    cy.get('[data-testid="compact-steps-trigger"]').click();
    cy.get('#step-agent').should('be.visible');
  });

  it('keeps a nested assistant message visible outside the collapsed summary', () => {
    submitMessage('nested_message');

    cy.get('[data-testid="compact-steps"]').should('have.length', 1);
    cy.get('[data-testid="compact-steps-trigger"]').should(
      'have.attr',
      'aria-expanded',
      'false'
    );

    // The assistant message produced inside a nested step is lifted to the
    // root: visible even though the summary is still collapsed.
    cy.contains('Nested answer').should('be.visible');

    // Expanding the summary shows only the steps, without duplicating the
    // message inside the accordion.
    cy.get('[data-testid="compact-steps-trigger"]').click();
    cy.get('[data-testid="compact-steps"]').should(
      'not.contain',
      'Nested answer'
    );
  });

  it("renders the last step's icon in the compact summary avatar", () => {
    submitMessage('icon');

    cy.get('[data-testid="compact-steps"]').should('have.length', 1);
    cy.get('[data-testid="compact-steps"]').find('img').should('not.exist');
  });

  it('shows the active step name while running', () => {
    submitMessage('slow');

    cy.get('[data-testid="compact-steps-trigger"]')
      .should('contain', 'Using slow_tool')
      .and('have.class', 'loading-shimmer');

    cy.get('[data-testid="compact-steps-trigger"]').should(
      'contain',
      'Used 2 steps'
    );
  });

  it('marks an error summary accessibly without auto-expanding', () => {
    submitMessage('error');

    cy.get('[data-testid="compact-steps"]').should('have.length', 1);
    cy.get('[data-testid="compact-steps-trigger"]')
      .should('have.class', 'text-red-500')
      .and('contain', 'An error occurred')
      .and('have.attr', 'aria-expanded', 'false');
    cy.get('#step-failing_tool').should('not.exist');

    cy.get('[data-testid="compact-steps-trigger"]').click();
    cy.get('#step-failing_tool').should('be.visible');
  });
});
