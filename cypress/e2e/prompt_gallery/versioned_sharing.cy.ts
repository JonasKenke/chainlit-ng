const login = (username: 'alice' | 'bob') =>
  cy.request({
    method: 'POST',
    url: '/login',
    form: true,
    body: { username, password: username }
  });

const createPrompt = (title: string, content: string) =>
  cy.request('POST', '/project/prompts', { title, content });

const sharePrompt = (id: string) =>
  cy.request('POST', `/project/prompts/${id}/share`, { isShared: true });

const createSkill = (name: string, content: string) =>
  cy.request('POST', '/project/skills', {
    name,
    description: 'Shared release workflow',
    content
  });

const shareSkill = (id: string) =>
  cy.request('POST', `/project/skills/${id}/share`, { isShared: true });

const copyFor = (sourceId: string) =>
  cy
    .request('/project/prompts')
    .then((response) =>
      response.body.find(
        (prompt: { sourcePromptId?: string }) =>
          prompt.sourcePromptId === sourceId
      )
    );

const skillCopyFor = (sourceId: string) =>
  cy
    .request('/project/skills')
    .then((response) =>
      response.body.find(
        (skill: { sourcePromptId?: string }) =>
          skill.sourcePromptId === sourceId
      )
    );

describe('Versioned sharing', () => {
  it('adds a prompt, updates it, then downgrades without re-notifying', () => {
    cy.intercept('POST', '/project/prompts/*/sync').as('syncPrompt');

    login('alice');
    createPrompt('Release checklist', 'Step one').then((created) => {
      const sourceId = created.body.id;
      sharePrompt(sourceId);

      login('bob');
      cy.visit(`/prompt/${sourceId}`);
      cy.get('[data-testid="prompt-share-add"]')
        .should('contain', 'Add to my gallery')
        .click();
      cy.location('pathname').should('eq', '/');

      login('alice');
      cy.request('PUT', `/project/prompts/${sourceId}`, {
        content: 'Step one\nStep two'
      });

      login('bob');
      copyFor(sourceId).then((copy) => {
        expect(copy.pinnedVersion).to.equal(1);
        cy.visit('/');
        cy.get('#prompt-gallery-open').click();

        cy.get(`[data-testid="prompt-versions-${copy.id}"]`).should(
          'contain',
          'v1'
        );
        cy.get(`[data-testid="prompt-update-chip-${copy.id}"]`)
          .should('contain', 'Update available v2')
          .click();

        cy.get('[data-testid="prompt-diff-dialog"]').should('be.visible');
        cy.get('[data-testid="prompt-diff-dialog"]').should(
          'contain',
          'Step two'
        );
        cy.screenshot('prompt-diff-v2');
        cy.get('[data-testid="prompt-diff-update"]')
          .should('contain', 'Update to v2')
          .click();
        cy.wait('@syncPrompt');

        cy.get(`[data-testid="prompt-versions-${copy.id}"]`).should(
          'contain',
          'v2'
        );
        cy.get(`[data-testid="prompt-update-chip-${copy.id}"]`).should(
          'not.exist'
        );

        cy.request(`/project/prompt/share/${sourceId}/versions`).then(
          (response) => {
            const v1 = response.body.find(
              (version: { version: number }) => version.version === 1
            );
            expect(v1, 'source v1').to.exist;

            cy.get(`[data-testid="prompt-versions-${copy.id}"]`).click();
            cy.get('[data-testid="prompt-diff-version"]').click();
            cy.get(
              `[data-testid="prompt-diff-version-option-${v1.id}"]`
            ).click();
            cy.get('[data-testid="prompt-diff-update"]')
              .should('contain', 'Downgrade to v1')
              .click();
            cy.wait('@syncPrompt');

            // v2 was acknowledged when the toast appeared; a deliberate
            // downgrade does not re-notify for the same source version.
            cy.request('POST', '/project/prompts/updates/ack', {
              promptId: copy.id,
              version: 2
            });
            cy.get(`[data-testid="prompt-versions-${copy.id}"]`).should(
              'contain',
              'v1'
            );
            cy.get(`[data-testid="prompt-update-chip-${copy.id}"]`).should(
              'not.exist'
            );
            cy.screenshot('prompt-downgraded-acknowledged');
          }
        );
      });
    });
  });

  it('keeps an imported skill disabled until reviewed, then supports update and downgrade', () => {
    const skillName = `release-skill-${Date.now().toString(36)}`;
    cy.intercept('PUT', '/project/skills/*').as('updateSkill');
    cy.intercept('POST', '/project/skills/*/update-from-source').as(
      'updateFromSource'
    );

    login('alice');
    createSkill(skillName, 'Check the changelog first.').then((created) => {
      const sourceId = created.body.id;
      shareSkill(sourceId);

      login('bob');
      cy.visit(`/skill/${sourceId}`);
      cy.get('[data-testid="skill-share-add"]')
        .should('contain', 'Add to my library')
        .click();
      cy.get('[data-testid="skill-share-added"]').should('be.visible');
      cy.get('[data-testid="skill-share-add"]').click();
      cy.location('pathname').should('eq', '/');

      login('alice');
      cy.request('PUT', `/project/skills/${sourceId}`, {
        content: 'Check the changelog and release notes first.'
      });

      login('bob');
      skillCopyFor(sourceId).then((copy) => {
        expect(copy.enabled).to.equal(false);
        expect(copy.pinnedVersion).to.equal(1);

        cy.visit('/');
        cy.get('#prompt-gallery-open').click();
        cy.contains('[role="tab"]', 'Skills').click();

        const row = () => cy.contains('[data-testid="skill-row"]', skillName);

        row().should('contain', 'Disabled. Review before enabling.');
        row()
          .find('[role="switch"]')
          .should('have.attr', 'aria-label', `Enable ${skillName}`)
          .click();
        cy.get('[data-testid="skill-review-dialog"]').should('be.visible');
        cy.screenshot('skill-review-before-enable');
        cy.get('[data-testid="skill-review-dialog"]')
          .contains('button', 'Cancel')
          .click();
        row()
          .find('[role="switch"]')
          .should('have.attr', 'aria-checked', 'false');

        row().find('[role="switch"]').click();
        cy.get('[data-testid="skill-review-dialog"]')
          .contains('button', 'Enable skill')
          .should('not.be.disabled')
          .click();
        cy.wait('@updateSkill');
        row()
          .find('[role="switch"]')
          .should('have.attr', 'aria-checked', 'true');
        cy.screenshot('skills-enabled-update-chip');

        cy.get(`[data-testid="skill-update-chip-${copy.id}"]`)
          .should('contain', 'Update available v2')
          .click();
        cy.get('[data-testid="skill-update-submit"]')
          .should('contain', 'Update to v2')
          .click();
        cy.wait('@updateFromSource');
        cy.get('[data-testid="skill-update-dialog"]').should('not.exist');
        cy.get(`[data-testid="skill-update-chip-${copy.id}"]`).should(
          'not.exist'
        );

        cy.request(`/project/skill/share/${sourceId}/versions`).then(
          (response) => {
            const v1 = response.body.find(
              (version: { version: number }) => version.version === 1
            );
            expect(v1, 'source skill v1').to.exist;

            cy.get(`[data-testid="skill-versions-${copy.id}"]`).click();
            cy.get('[data-testid="skill-update-version"]').click();
            cy.get(
              `[data-testid="skill-update-version-option-${v1.id}"]`
            ).click();
            cy.get('[data-testid="skill-update-submit"]')
              .should('contain', 'Downgrade to v1')
              .click();
            cy.wait('@updateFromSource');
            cy.get('[data-testid="skill-update-dialog"]').should('not.exist');

            // Source imports return disabled, and v2 stays offered.
            row()
              .find('[role="switch"]')
              .should('have.attr', 'aria-checked', 'false');
            cy.get(`[data-testid="skill-update-chip-${copy.id}"]`).should(
              'contain',
              'Update available v2'
            );
            cy.screenshot('skill-downgraded-disabled-update-still-offered');
          }
        );
      });
    });
  });
});

describe('Narrow viewport', () => {
  it('keeps the library and prompt diff inside a phone viewport', () => {
    login('bob');
    cy.viewport('iphone-x');
    cy.visit('/');
    cy.get('#prompt-gallery-open').click();
    cy.get('[role="dialog"]').should('be.visible');
    cy.screenshot('library-mobile');
    cy.document().then((document) => {
      expect(document.documentElement.scrollWidth).to.be.at.most(
        document.documentElement.clientWidth + 1
      );
    });

    cy.get('[data-testid^="prompt-versions-"]').first().click();
    cy.get('[data-testid="prompt-diff-dialog"]').should('be.visible');
    cy.screenshot('prompt-diff-mobile');
    cy.document().then((document) => {
      expect(document.documentElement.scrollWidth).to.be.at.most(
        document.documentElement.clientWidth + 1
      );
    });
  });
});
