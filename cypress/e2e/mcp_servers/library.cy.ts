const STORAGE_KEY = 'mcp_storage_key';

const docsMcp = {
  name: 'docs',
  tools: [{ name: 'echo' }],
  prompts: [
    {
      name: 'standup',
      description: 'Daily standup',
      arguments: [{ name: 'team', required: true, description: 'Team name' }]
    },
    { name: 'plain', description: 'No arguments' }
  ],
  resources: [
    { uri: 'file:///readme.md', name: 'README', mimeType: 'text/markdown' }
  ],
  isUserProvided: false,
  type: 'streamable-http'
};

/**
 * Seed a stored server so the app reconnects it by name on load; the stubbed
 * `POST /mcp` then publishes the prompts and resources under test.
 */
const visitWithDocs = (mcp: Record<string, unknown> = docsMcp, wait = true) => {
  cy.intercept('POST', '**/mcp', {
    statusCode: 200,
    body: { success: true, mcp }
  }).as('connectMcp');

  cy.visit('/', {
    onBeforeLoad(win) {
      win.localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify([
          { name: 'docs', status: 'failed', type: 'streamable-http' }
        ])
      );
    }
  });

  cy.get('#chat-input').should('exist');
  if (wait) {
    cy.wait('@connectMcp');
    cy.get('#mcp-library-open').should('be.visible');
  }
};

const openLibrary = () => {
  cy.get('#mcp-library-open').click();
  cy.get('#mcp-library').should('be.visible');
};

describe('MCP library', () => {
  it('inserts a prompt without arguments and never sends it', () => {
    cy.intercept('POST', '**/mcp/prompt', {
      statusCode: 200,
      body: { text: 'Plain prompt' }
    }).as('prompt');

    visitWithDocs();
    openLibrary();
    cy.get('#mcp-library').contains('button', 'plain').click();

    cy.wait('@prompt')
      .its('request.body')
      .should((body) => {
        expect(body.name).to.eq('docs');
        expect(body.prompt).to.eq('plain');
        expect(body).to.not.have.property('arguments');
      });

    // Rendered text lands in the composer only; nothing is auto-sent.
    cy.get('#chat-input').should('have.value', 'Plain prompt');
    cy.contains('Prompt inserted into the composer').should('be.visible');
    cy.get('#mcp-library').should('not.exist');
    cy.get('.step').should('not.exist');
  });

  it('blocks empty required arguments and sends the entered values', () => {
    cy.intercept('POST', '**/mcp/prompt', {
      statusCode: 200,
      body: { text: 'Team core' }
    }).as('prompt');

    visitWithDocs();
    openLibrary();
    cy.get('#mcp-library').contains('button', 'standup').click();

    // The argument form replaces the list and marks the field required.
    cy.get('#mcp-library').contains('Daily standup').should('be.visible');
    cy.get('#mcp-prompt-arg-team').should('have.attr', 'required');

    cy.get('#mcp-library').contains('button', 'Insert').click();
    // Native form validation keeps the dialog open and sends nothing.
    cy.get('#mcp-prompt-arg-team').should('be.visible');
    cy.get('@prompt.all').should('have.length', 0);

    cy.get('#mcp-prompt-arg-team').type('core');
    cy.get('#mcp-library').contains('button', 'Insert').click();

    cy.wait('@prompt')
      .its('request.body')
      .should((body) => {
        expect(body.prompt).to.eq('standup');
        expect(body.arguments).to.deep.eq({ team: 'core' });
      });

    cy.get('#chat-input').should('have.value', 'Team core');
    cy.get('#mcp-library').should('not.exist');
  });

  it('reads a resource for the selected server and confirms it', () => {
    cy.intercept('POST', '**/mcp/resource', {
      statusCode: 200,
      body: { element: { id: 'element-1', type: 'file' } }
    }).as('resource');

    visitWithDocs();
    openLibrary();
    cy.get('#mcp-library').contains('[role="tab"]', 'Resources').click();
    cy.get('#mcp-library').contains('button', 'README').click();

    cy.wait('@resource')
      .its('request.body')
      .should((body) => {
        expect(body.name).to.eq('docs');
        expect(body.uri).to.eq('file:///readme.md');
      });

    cy.contains('Resource added to the chat').should('be.visible');
    cy.get('#mcp-library').should('not.exist');
  });

  it('hides the picker when the server publishes no prompts or resources', () => {
    visitWithDocs(
      {
        name: 'docs',
        tools: [{ name: 'echo' }],
        isUserProvided: false,
        type: 'streamable-http'
      },
      false
    );

    // The server is connected: the MCP button is there, the library is not.
    cy.wait('@connectMcp');
    cy.get('.lucide-plug').should('be.visible');
    cy.get('#mcp-library-open').should('not.exist');
  });
});
