describe('Message limit', () => {
  it('sends a multi-megabyte pasted message', () => {
    cy.visit('/');
    // The welcome message is sent over the socket, so receiving it means the
    // server session exists and the attachment upload will find it.
    cy.contains('.step', 'Ready', { timeout: 10000 }).should('exist');

    cy.fixture('state_of_the_union.txt', 'utf-8').as('txtFile');
    cy.get('#upload-button-input').selectFile('@txtFile', { force: true });
    cy.get('#attachments').should('contain', 'state_of_the_union.txt');

    // A paste inserts the whole value at once. Setting the textarea value
    // through the native setter produces the same composer state and avoids
    // typing 1.2M characters one keystroke at a time.
    const size = 1_200_000;
    const payload = 'a'.repeat(size);

    cy.get('#chat-input').then(($input) => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        'value'
      )!.set!;
      setter.call($input[0], payload);
      $input[0].dispatchEvent(new Event('input', { bubbles: true }));
    });

    cy.get('#attachments').should('contain', 'state_of_the_union.txt');
    cy.get('#chat-submit').should('not.be.disabled').click({ force: true });

    cy.get('[data-sonner-toast]', { timeout: 10000 })
      .should('be.visible')
      .and('contain.text', 'Message is too large');

    // Nothing was sent: no optimistic user message, no server echo.
    cy.get('.step[data-step-type="user_message"]').should('not.exist');
    cy.contains('Echo length').should('not.exist');
    cy.location('pathname').should('eq', '/');

    // The refusal keeps the draft and the attachment.
    cy.get('#chat-input').should(($input) => {
      expect(($input[0] as HTMLTextAreaElement).value.length).to.eq(size);
    });
    cy.get('#attachments').should('contain', 'state_of_the_union.txt');
  });
});
