import { submitMessage } from '../../support/testUtils';

const login = () => {
  cy.intercept('/auth/header', (req) => {
    req.headers['test-header'] = 'question-card';
    req.reply();
  }).as('auth');
};

const questionCard = () => cy.get('[data-testid="question-card"]');

describe('Question card reload and share', () => {
  it('shows an answered free-text card read-only after reload and redacts it in the share view', () => {
    login();
    cy.visit('/');
    submitMessage('ask-text');

    questionCard().should('contain', 'What should I name the release?');
    cy.screenshot('question-card-pending');
    questionCard().find('textarea').focus().type('Kestrel');
    questionCard().contains('button', 'Submit').click();
    cy.contains('answered privately').should('be.visible');

    cy.location('pathname').then((path) => {
      const threadId = path.split('/').pop()!;

      cy.step('Owner reload renders the answered card read-only');
      cy.reload();
      questionCard().should('contain', 'What should I name the release?');
      questionCard().should('contain', 'Kestrel');
      questionCard().find('textarea').should('not.exist');
      questionCard().find('input[type="radio"]').should('not.exist');
      questionCard().contains('button', 'Submit').should('not.exist');

      cy.step('Share JSON redacts the typed answer and drops identifiers');
      cy.request('PUT', '/project/thread/share', {
        threadId,
        isShared: true
      })
        .its('status')
        .should('eq', 200);
      cy.request(`/project/share/${threadId}`).then((response) => {
        expect(response.status).to.eq(200);
        const body = JSON.stringify(response.body);
        expect(body).not.to.contain('Kestrel');

        const step = response.body.steps.find(
          (item: any) => item.metadata?.approval?.kind === 'question'
        );
        expect(step.metadata.approval.answer).to.deep.eq({
          text: null,
          redacted: true
        });
        expect(step.metadata.approval.prompt).to.contain('release');
        expect(step.metadata.approval.allowFreeText).to.eq(true);
        for (const hidden of [
          'requestedBy',
          'decidedBy',
          'decisionRequestId',
          'expiresAt',
          'revision'
        ]) {
          expect(step.metadata.approval).not.to.have.property(hidden);
        }
      });

      cy.step('Shared card says answered privately without the text');
      cy.visit(`/share/${threadId}`);
      questionCard().should('contain', 'Answered privately');
      questionCard().should('not.contain', 'Kestrel');
      questionCard().find('textarea').should('not.exist');
      questionCard().should('have.attr', 'aria-live', 'polite');

      cy.step('The read-only card stays reachable on a mobile viewport');
      cy.viewport('iphone-x');
      cy.visit(`/share/${threadId}`);
      questionCard().should('be.visible');
      questionCard().should('contain', 'Answered privately');
      cy.screenshot('question-card-mobile-readonly');
    });
  });

  it('keeps the server option label in a shared card', () => {
    login();
    cy.visit('/');
    submitMessage('ask-option');

    questionCard().should('contain', 'Which environment should I deploy to?');
    questionCard().find('input[type="radio"]').first().click();
    questionCard().find('input[type="radio"]').first().should('be.checked');
    questionCard()
      .contains('button', 'Submit')
      .should('not.be.disabled')
      .click();
    cy.contains('answered option=staging label=Staging').should('be.visible');

    cy.step('Owner reload renders the option label read-only');
    cy.reload();
    questionCard().should('contain', 'Staging');
    questionCard().find('input[type="radio"]').should('not.exist');

    cy.location('pathname').then((path) => {
      const threadId = path.split('/').pop()!;
      cy.request('PUT', '/project/thread/share', {
        threadId,
        isShared: true
      })
        .its('status')
        .should('eq', 200);
      cy.request(`/project/share/${threadId}`).its('status').should('eq', 200);

      cy.intercept('GET', `/project/share/${threadId}`).as('share');
      cy.visit(`/share/${threadId}`);
      cy.wait('@share').its('response.statusCode').should('eq', 200);
      questionCard().should('contain', 'Staging');
      questionCard().should('not.contain', 'Answered privately');
      questionCard().find('input[type="radio"]').should('not.exist');
    });
  });
});
