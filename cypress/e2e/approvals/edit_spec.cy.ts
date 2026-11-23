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

  it('edits a schema field in the form and renders the diff', () => {
    cy.intercept('POST', approveUrl).as('decide');
    login();
    cy.visit('/');

    submitMessage('schema');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('be.visible')
      .click();

    cy.get('[data-testid="approval-card"]').then(($card) => {
      const approvalId = $card.attr('data-approval-id');
      const to = `#approval-${approvalId}-to`;
      cy.get(to).should('have.value', 'person@example.com');
      cy.get(to).clear();
      cy.get(to).type('form@example.com');
    });
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Save & approve')
      .click();

    cy.wait('@decide').its('response.statusCode').should('eq', 200);
    cy.get('[data-testid="approval-diff"]')
      .should('contain', 'To')
      .and('contain', 'person@example.com')
      .and('contain', 'form@example.com');
    cy.contains('edited to form@example.com');
    cy.screenshot('approval-form-edited');
  });

  it('edits a schema field and renders the diff', () => {
    cy.intercept('POST', approveUrl).as('decide');
    login();
    cy.visit('/');

    submitMessage('json');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('be.visible')
      .click();

    editArguments('{"to": "other@example.com", "options": {"tone": "brief"}}');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Save edit')
      .click();

    cy.wait('@decide').its('response.statusCode').should('eq', 200);
    cy.get('[data-testid="approval-card"]').should(
      'contain',
      'Approved with edits'
    );
    cy.get('[data-testid="approval-diff"]')
      .should('contain', 'To')
      .and('contain', 'person@example.com')
      .and('contain', 'other@example.com');
    cy.screenshot('approval-edited-diff');
    cy.get('[data-testid="approval-card"]').find('button').should('not.exist');
    cy.contains('edited to other@example.com');
  });

  it('shows the invalid field path when the server rejects an edit', () => {
    cy.intercept('POST', approveUrl).as('decide');
    login();
    cy.visit('/');

    submitMessage('json');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('be.visible')
      .click();

    editArguments(
      '{"to": "other@example.com", "options": {"tone": "brief"}, "unknownField": true}'
    );
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Save edit')
      .click();

    cy.wait('@decide').its('response.statusCode').should('eq', 422);
    cy.contains('editedArguments.unknownField (extra_forbidden)');
    cy.get('[data-testid="approval-diff"]').should('not.exist');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Edit')
      .should('be.visible');
  });

  it('requires a comment before requesting changes', () => {
    cy.intercept('POST', approveUrl).as('decide');
    login();
    cy.visit('/');

    submitMessage('ask');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Request changes')
      .should('be.visible')
      .click();

    cy.get('textarea[aria-label="Comment"]').should('be.visible').as('comment');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Request changes')
      .should('be.disabled');
    cy.get('@comment').type('Use the finance alias.');
    cy.get('[data-testid="approval-card"]')
      .contains('button', 'Request changes')
      .should('not.be.disabled')
      .click();

    cy.wait('@decide').its('response.statusCode').should('eq', 200);
    cy.get('[data-testid="approval-card"]').should(
      'contain',
      'Changes requested'
    );
    cy.get('[data-testid="approval-comment"]').should(
      'contain',
      'Use the finance alias.'
    );
    cy.get('[data-testid="approval-card"]').find('button').should('not.exist');
    cy.screenshot('approval-request-changes');
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
    cy.get('[data-testid="approval-card"]')
      .should('not.contain', 'approvals-user')
      .find('time')
      .should('not.exist');
    cy.get('[data-testid="approval-card"]').find('button').should('not.exist');
    cy.contains('not executed: reject');
  });
});
