import { submitMessage } from '../../support/testUtils';

const withoutCode = ($el: JQuery<HTMLElement>) => {
  const clone = $el.clone();
  clone.find('code').remove();
  return clone.text();
};

describe('LaTeX', () => {
  it('loads the KaTeX stylesheet matching the JS version', () => {
    cy.visit('/');
    cy.get('link[rel="stylesheet"][href*="katex"]')
      .should('have.attr', 'href')
      .and('include', 'katex@0.16.47/dist/katex.min.css');
  });

  it('renders \\(...\\) and \\[...\\] delimiters and keeps dollar math', () => {
    cy.visit('/');
    submitMessage('all');

    cy.get('.step').last().should('contain.text', 'Ohm:');

    // Inline/display TeX delimiters plus the unchanged $ / $$ math.
    cy.get('.step').last().find('.katex').should('have.length', 4);
    cy.get('.step').last().find('.katex-display').should('have.length', 2);

    cy.get('.step')
      .last()
      .then(($el) => {
        const text = withoutCode($el);
        expect(text).not.to.contain('\\(');
        expect(text).not.to.contain('\\[');
      });

    // Code spans and fences keep their delimiters literally.
    cy.get('.step').last().find('code').first().should('have.text', '\\(x\\)');
    cy.get('.step')
      .last()
      .find('code')
      .last()
      .should('contain.text', '\\[y\\]');
    cy.get('.step')
      .last()
      .find('code')
      .last()
      .find('.katex')
      .should('not.exist');
  });

  it('keeps an unclosed delimiter as text while streaming, then renders it', () => {
    cy.visit('/');
    submitMessage('stream');

    cy.get('.step').last().should('contain.text', '( x + 1');
    cy.get('.step').last().find('.katex').should('not.exist');

    cy.get('.step').last().find('.katex', { timeout: 10000 }).should('exist');
    cy.get('.step')
      .last()
      .should(($el) => {
        expect(withoutCode($el)).not.to.contain('\\(');
        expect(withoutCode($el)).not.to.contain('\\)');
      });
  });
});
