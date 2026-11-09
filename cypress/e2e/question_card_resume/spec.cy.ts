import { submitMessage } from '../../support/testUtils';

const login = () => {
  cy.intercept('/auth/header', (req) => {
    req.headers['test-header'] = 'question-card';
    req.reply();
  }).as('auth');
};

const questionCard = () => cy.get('[data-testid="question-card"]');

describe('Question card reload with thread resume', () => {
  it('renders a hydrated answered question read-only after reload', () => {
    login();
    cy.visit('/');
    submitMessage('ask-text');

    questionCard().should('contain', 'What should I name the release?');
    questionCard().find('textarea').focus().type('Kestrel');
    questionCard().contains('button', 'Submit').click();
    cy.contains('answered privately').should('be.visible');

    cy.reload();
    questionCard().should('contain', 'What should I name the release?');
    questionCard().should('contain', 'Answered');
    questionCard().should('contain', 'Kestrel');
    questionCard().find('textarea').should('not.exist');
    questionCard().find('input[type="radio"]').should('not.exist');
    questionCard().contains('button', 'Submit').should('not.exist');
  });

  it('renders a hydrated pending question read-only after reload', () => {
    login();
    cy.visit('/');
    submitMessage('ask-text');

    questionCard().should('contain', 'What should I name the release?');
    questionCard().find('textarea').should('be.visible');
    cy.location('pathname').should('match', /\/thread\//);

    cy.reload();
    questionCard().should('be.visible');
    questionCard().find('textarea').should('not.exist');
    questionCard().find('input[type="radio"]').should('not.exist');
    questionCard().contains('button', 'Submit').should('not.exist');
  });
});
