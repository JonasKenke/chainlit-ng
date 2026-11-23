import { submitMessage } from '../../support/testUtils';

const login = () => {
  cy.intercept('/auth/header', (req) => {
    req.headers['test-header'] = 'question';
    req.reply();
  }).as('auth');
};

const answer = '**/project/question';

describe('Question card', () => {
  it('shows the live card with CoT hidden and submits an option', () => {
    cy.intercept('POST', answer).as('answer');
    login();
    cy.visit('/');

    submitMessage('option');

    cy.get('[data-testid="question-card"]').should('have.length', 1);
    cy.get('[data-testid="question-card"]').should(
      'contain',
      'Which environment should I deploy to?'
    );
    // The step that binds the ask is hidden by [UI] cot="hidden".
    cy.get('#question-step').should('not.exist');

    cy.get('[data-testid="question-card"]')
      .find('input[type="radio"]')
      .should('have.length', 2)
      .first()
      .check();

    cy.get('[data-testid="question-card"]')
      .contains('button', 'Submit')
      .click();

    cy.wait('@answer').its('response.statusCode').should('eq', 200);
    cy.get('[data-testid="question-card"]').should('not.contain', 'Answered');
    cy.get('[data-testid="question-card"]').should('contain', 'Staging');
    cy.get('[data-testid="question-card"]').should('not.contain', 'question-user');
    cy.get('[data-testid="question-card"]').find('time').should('not.exist');
    cy.contains('answered option=staging label=Staging').should('be.visible');
  });

  it('submits a free-text answer', () => {
    cy.intercept('POST', answer).as('answer');
    login();
    cy.visit('/');

    submitMessage('text');

    cy.get('[data-testid="question-card"]').should(
      'contain',
      'What should I name the release?'
    );
    cy.get('[data-testid="question-card"]').find('textarea').type('Kestrel');
    cy.get('[data-testid="question-card"]')
      .contains('button', 'Submit')
      .click();

    cy.wait('@answer').its('response.statusCode').should('eq', 200);
    cy.get('[data-testid="question-card"]').should('not.contain', 'Answered');
    cy.get('[data-testid="question-card"]').should('contain', 'Kestrel');
    cy.contains('answered text=Kestrel').should('be.visible');
  });
});
