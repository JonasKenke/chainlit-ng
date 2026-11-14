import { submitMessage } from '../../support/testUtils';

const login = () => {
  cy.intercept('/auth/header', (req) => {
    req.headers['test-header'] = 'approvals';
    req.reply();
  }).as('auth');
};

const approveUrl = '**/project/approval';

const editArguments = (value: string) => {
  cy.get('textarea[aria-label="Edited arguments JSON"]')
    .should('be.visible')
    .clear();
  cy.get('textarea[aria-label="Edited arguments JSON"]').type(value, {
    parseSpecialCharSequences: false
  });
};

describe('Approval edit and reject', () => {
  it('hides Edit when the server does not allow edits', () => {
    login();
    cy.visit('/');

    submitMessage('ask');

    cy.get('[data-testid="approval-card"]').should('have.length', 1);
    cy.get('[data-testid="approval-card"]').should(
      'contain',
      'Awaiting decision'
    );
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Approve')
      .should('be.visible');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Reject')
      .should('be.visible');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('not.exist');
  });

  it('edits a schema field and renders the diff', () => {
    cy.intercept('POST', approveUrl).as('decide');
    login();
    cy.visit('/');

    submitMessage('schema');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('be.visible')
      .click();

    editArguments('{"to": "other@example.com"}');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Save edit')
      .click();

    cy.wait('@decide').its('response.statusCode').should('eq', 200);
    cy.get('[data-testid="approval-card"]').should('contain', 'Edited');
    cy.get('[data-testid="approval-diff"]').should(
      'contain',
      '~ to: "person@example.com" → "other@example.com"'
    );
    cy.get('[data-testid="approval-card"]').find('button').should('not.exist');
    cy.contains('edited to other@example.com');
  });

  it('shows the invalid field path when the server rejects an edit', () => {
    cy.intercept('POST', approveUrl).as('decide');
    login();
    cy.visit('/');

    submitMessage('schema');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('be.visible')
      .click();

    editArguments('{"to": "other@example.com", "unknownField": true}');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Save edit')
      .click();

    cy.wait('@decide').its('response.statusCode').should('eq', 422);
    cy.contains('editedArguments.unknownField (extra_forbidden)');
    cy.get('[data-testid="approval-card"]').should(
      'contain',
      'Awaiting decision'
    );
    cy.get('[data-testid="approval-diff"]').should('not.exist');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('be.visible');
  });

  it('renders a reject decision as rejected without decision affordances', () => {
    cy.intercept('POST', approveUrl).as('decide');
    login();
    cy.visit('/');

    submitMessage('ask');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Reject')
      .should('be.visible')
      .click();

    cy.wait('@decide').its('response.statusCode').should('eq', 200);
    cy.get('[data-testid="approval-card"]').should('contain', 'Rejected');
    cy.get('[data-testid="approval-card"]').find('button').should('not.exist');
    cy.contains('not executed: reject');
  });
});
