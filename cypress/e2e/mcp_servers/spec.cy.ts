const STORAGE_KEY = 'mcp_storage_key';

const openMcpDialog = () => {
  cy.get('#chat-input').should('exist');
  cy.get('.lucide-plug').should('be.visible');
  cy.get('.lucide-plug').closest('button').click();
  cy.get('#mcp-servers').should('be.visible');
};

describe('MCP named servers', () => {
  it('lists configured servers and connects one by name', () => {
    cy.intercept('POST', '**/mcp', {
      statusCode: 200,
      body: {
        success: true,
        mcp: {
          name: 'named-sse',
          tools: [{ name: 'echo' }],
          isUserProvided: false,
          type: 'sse'
        }
      }
    }).as('connectNamed');

    cy.visit('/');
    cy.get('#chat-input').should('exist');
    openMcpDialog();

    cy.contains('Configured servers').should('be.visible');
    cy.contains('named-sse').should('be.visible');
    cy.contains('named-stdio').should('be.visible');
    // The stdio form took a client-supplied command; the port removed it.
    cy.get('#mcp-servers').find('input[name="command"]').should('not.exist');
    cy.get('#mcp-servers').find('textarea').should('not.exist');

    cy.contains('named-sse').closest('div.border').find('button').click();

    cy.wait('@connectNamed')
      .its('request.body')
      .should((body) => {
        expect(body.name).to.eq('named-sse');
        expect(body).to.not.have.property('command');
        expect(body).to.not.have.property('clientType');
        expect(body).to.not.have.property('url');
      });

    // onSuccess opens the list tab with the connected server.
    cy.get('#mcp-servers').contains('h3', 'named-sse').should('be.visible');
    cy.get('#mcp-servers')
      .contains('h3', 'named-sse')
      .closest('div.border')
      .within(() => {
        cy.get('.bg-green-500').should('exist');
        cy.contains(/^sse$/).should('exist');
        cy.contains('echo').should('exist');
      });
  });

  it('hides the user-server form when user_servers is disabled', () => {
    cy.visit('/');
    cy.get('#chat-input').should('exist');
    openMcpDialog();

    cy.contains('Configured servers').should('be.visible');
    cy.contains('Add your own server').should('not.exist');
    cy.get('#server-name').should('not.exist');
    cy.get('#server-url').should('not.exist');
  });

  it('migrates a stored pre-port entry and drops corrupt ones', () => {
    cy.intercept('POST', '**/mcp', {
      statusCode: 200,
      body: {
        success: true,
        mcp: {
          name: 'legacy-stdio',
          tools: [{ name: 'legacy-tool' }],
          isUserProvided: false,
          type: 'stdio'
        }
      }
    }).as('reconnectLegacy');

    cy.visit('/', {
      onBeforeLoad(win) {
        win.localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify([
            {
              name: 'legacy-stdio',
              tools: [{ name: 'legacy-tool' }],
              status: 'failed',
              clientType: 'stdio',
              command: 'npx legacy-server'
            },
            { name: '', status: 'failed' },
            { name: 'broken-http', clientType: 'streamable-http' },
            'not-an-object'
          ])
        );
      }
    });

    // The migrated entry reconnects by name, never by a client-supplied command.
    cy.wait('@reconnectLegacy')
      .its('request.body')
      .should((body) => {
        expect(body.name).to.eq('legacy-stdio');
        expect(body).to.not.have.property('command');
      });

    openMcpDialog();
    cy.get('#mcp-servers').contains('button', 'My MCPs').click();
    cy.get('#mcp-servers').contains('h3', 'legacy-stdio').should('be.visible');
    cy.get('#mcp-servers')
      .contains('No MCP servers connected')
      .should('not.exist');

    cy.window().should((win) => {
      const stored = JSON.parse(win.localStorage.getItem(STORAGE_KEY) || '[]');
      expect(stored).to.have.length(1);
      expect(stored[0]).to.include({ name: 'legacy-stdio', type: 'stdio' });
      expect(stored[0]).to.not.have.property('command');
      expect(stored[0]).to.not.have.property('clientType');
    });
  });
});
