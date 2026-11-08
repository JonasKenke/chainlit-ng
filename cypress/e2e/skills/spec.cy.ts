const row = (name: string) => cy.contains('[data-testid="skill-row"]', name);

describe('Skills', () => {
  it('creates, enables and keeps a skill after reload', () => {
    cy.intercept('POST', '/project/skills').as('createSkill');
    cy.intercept('PUT', '/project/skills/*').as('updateSkill');
    cy.intercept('GET', '/project/skills').as('listSkills');

    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').contains('Skills').should('be.visible');
    cy.get('#skill-new').click();
    cy.get('#skill-name').type('release-notes');
    cy.get('#skill-description').type(
      'Draft user-facing release notes from a changelog.'
    );
    cy.get('#skill-content').type('# Release notes\n\n- Group by type.', {
      parseSpecialCharSequences: false
    });
    cy.get('#skill-save').click();
    cy.wait('@createSkill');

    row('release-notes').should('be.visible');
    row('release-notes')
      .find('[role="switch"]')
      .should('have.attr', 'aria-checked', 'true');

    row('release-notes').find('[role="switch"]').click();
    cy.wait('@updateSkill');
    row('release-notes')
      .find('[role="switch"]')
      .should('have.attr', 'aria-checked', 'false');

    row('release-notes').find('[role="switch"]').click();
    cy.wait('@updateSkill');
    row('release-notes')
      .find('[role="switch"]')
      .should('have.attr', 'aria-checked', 'true');

    cy.reload();
    cy.get('#prompt-gallery-open').click();
    cy.wait('@listSkills');
    row('release-notes')
      .find('[role="switch"]')
      .should('have.attr', 'aria-checked', 'true');
  });

  it('imports disabled with provenance and shows backend errors', () => {
    cy.intercept('POST', '/project/skills/import').as('importSkill');
    cy.intercept('PUT', '/project/skills/*').as('updateSkill');

    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').contains('Skills').should('be.visible');

    cy.get('#skill-import-input').selectFile(
      'cypress/fixtures/skill-invalid.md',
      { force: true }
    );
    cy.get('#skill-error').should(
      'contain.text',
      'missing required frontmatter field'
    );

    cy.get('#skill-import-input').selectFile(
      'cypress/fixtures/skill-import.md',
      { force: true }
    );
    cy.wait('@importSkill');

    row('imported-one').within(() => {
      cy.contains('Imported').should('be.visible');
      cy.contains('skill-import.md').should('be.visible');
      cy.contains('Disabled — review before enabling').should('be.visible');
      cy.get('[role="switch"]').should('have.attr', 'aria-checked', 'false');
    });

    row('imported-one').find('[role="switch"]').click();
    cy.wait('@updateSkill');
    row('imported-one')
      .find('[role="switch"]')
      .should('have.attr', 'aria-checked', 'true');
  });

  it('edits and deletes a skill', () => {
    cy.intercept('PUT', '/project/skills/*').as('updateSkill');
    cy.intercept('DELETE', '/project/skills/*').as('deleteSkill');

    cy.get('#prompt-gallery-open').click();
    row('release-notes').find('[aria-label="Edit skill"]').click();
    cy.get('#skill-description').clear();
    cy.get('#skill-description').type('Updated description.');
    cy.get('#skill-save').click();
    cy.wait('@updateSkill');
    row('release-notes').contains('Updated description.').should('be.visible');

    row('release-notes').find('[aria-label="Delete skill"]').click();
    cy.wait('@deleteSkill');
    row('release-notes').should('not.exist');
  });
});
