import {
  loadCopilotScript,
  mountCopilotWidget,
  openCopilot,
  submitMessage
} from '../../support/testUtils';

function login() {
  return cy.request({
    method: 'GET',
    url: '/auth/custom',
    followRedirect: false
  });
}

function getToken() {
  return cy.request({
    method: 'GET',
    url: '/auth/token',
    followRedirect: false
  });
}

function shouldShowGreetingMessage() {
  it('should show greeting message', () => {
    cy.get('.step').should('exist');
    cy.get('.step').should('contain', 'Hello');
  });
}

function shouldSendMessageAndRecieveAnswer() {
  it('should send message and receive answer', () => {
    cy.get('.step').should('contain', 'Hello');

    const testMessage = 'Test message from custom auth';
    submitMessage(testMessage);

    cy.get('.step').should('contain', 'Echo:');
    cy.get('.step').should('contain', testMessage);
  });
}

describe('Custom Auth', () => {
  describe('when unauthenticated', () => {
    beforeEach(() => {
      cy.intercept('GET', '/user').as('user');
    });

    it('should attempt to and not have permission to access /user', () => {
      cy.wait('@user').then((interception) => {
        expect(interception.response.statusCode).to.equal(401);
      });
    });

    it('should redirect to login dialog', () => {
      cy.location('pathname').should('eq', '/login');
    });
  });

  describe('authenticating via custom endpoint', () => {
    beforeEach(() => {
      login().then((response) => {
        expect(response.status).to.equal(200);
        // Verify cookie is set in response headers
        expect(response.headers).to.have.property('set-cookie');
        const cookies = Array.isArray(response.headers['set-cookie'])
          ? response.headers['set-cookie']
          : [response.headers['set-cookie']];
        expect(cookies[0]).to.contain('access_token');
      });
    });

    const shouldBeLoggedIn = () => {
      it('should not be on /login', () => {
        cy.location('pathname').should('not.contain', '/login');
      });

      shouldShowGreetingMessage();
    };

    shouldBeLoggedIn();

    it('should request and have access to /user', () => {
      cy.intercept('GET', '/user').as('user');
      cy.wait('@user').then((interception) => {
        expect(interception.response.statusCode).to.equal(200);
      });
    });

    shouldSendMessageAndRecieveAnswer();

    describe('after reloading', () => {
      beforeEach(() => {
        cy.reload();
      });

      shouldBeLoggedIn();
    });
  });
});

describe('Copilot Token', { includeShadowDom: true }, () => {
  beforeEach(() => {
    cy.location('pathname').should('eq', '/login');

    loadCopilotScript();
  });

  describe('when unauthenticated', () => {
    it('should throw error about missing authentication token', () => {
      mountCopilotWidget();
      openCopilot();
      cy.get('#chainlit-copilot-chat').should(
        'contain',
        'No authentication token provided.'
      );
    });
  });

  describe('authenticating via custom endpoint', () => {
    beforeEach(() => {
      getToken().then((response) => {
        expect(response.status).to.equal(200);

        const accessToken = response.body;
        expect(accessToken).to.not.be.null;

        mountCopilotWidget({ accessToken });
        openCopilot();
      });
    });

    shouldShowGreetingMessage();

    shouldSendMessageAndRecieveAnswer();
  });

  describe('when the auth cookie is blocked', () => {
    it('should show the blocked third-party cookie message', () => {
      // Cypress re-adds Set-Cookie from the browser network layer after response
      // modifiers, so return the same 200 body the real endpoint sends without
      // the header. That is what the browser sees when it drops the cookie.
      cy.intercept('POST', '/auth/jwt', {
        statusCode: 200,
        body: { success: true },
        headers: { 'content-type': 'application/json' }
      }).as('jwt');

      getToken().then((response) => {
        expect(response.status).to.equal(200);

        mountCopilotWidget({ accessToken: response.body });
      });

      cy.wait('@jwt');
      openCopilot();

      cy.get('#chainlit-copilot-chat').should(
        'contain',
        'blocking third-party cookies'
      );
      cy.get('#chainlit-copilot-chat').should(
        'contain',
        'same site as this page'
      );
    });

    it('should keep the generic error for a transient network failure', () => {
      cy.intercept('POST', '/auth/jwt', { forceNetworkError: true }).as('jwt');

      getToken().then((response) => {
        expect(response.status).to.equal(200);

        mountCopilotWidget({ accessToken: response.body });
      });

      cy.wait('@jwt');
      openCopilot();

      cy.get('#chainlit-copilot-chat .alert').should('exist');
      cy.get('#chainlit-copilot-chat').should(
        'not.contain',
        'blocking third-party cookies'
      );
    });
  });
});
