describe('Profile menu sizing', () => {
  for (const width of [1200, 320]) {
    for (const [field, name] of [
      ['display_name', 'Alexandria Catherine MontgomeryWorthingtonSomerset'],
      [
        'identifier',
        'alexandria.catherine.montgomery-worthington@very-long-company-name.example.com'
      ]
    ]) {
      it(`shows the complete ${field} without overflow at ${width}px`, () => {
        cy.viewport(width, 800);
        cy.intercept('GET', '/user', (request) => {
          request.continue((response) => {
            if (response.statusCode === 200) {
              response.body[field] = name;
            }
          });
        });
        cy.visit('/login');
        cy.get('#email').type('admin');
        cy.get('#password').type('admin{enter}');
        cy.location('pathname').should('eq', '/');
        cy.get('#user-nav-button').click();

        cy.get('[role="menu"][data-state="open"]', { timeout: 5000 })
          .should('be.visible')
          .should(($menu) => {
            const menu = $menu[0];
            const bounds = menu.getBoundingClientRect();
            expect(bounds.left).to.be.at.least(0);
            expect(bounds.right).to.be.at.most(width);
            expect(menu.scrollWidth).to.be.at.most(menu.clientWidth + 1);
          })
          .within(() => {
            cy.contains('p', name, { timeout: 5000 })
              .should('have.text', name)
              .should(($name) => {
                const label = $name[0];
                expect(label.scrollWidth).to.be.at.most(label.clientWidth + 1);
              });
            cy.contains('[role="menuitem"]', 'Logout').should('be.visible');
          });
      });
    }
  }
});
