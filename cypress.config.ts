import { defineConfig } from 'cypress';
import cypressSplit from 'cypress-split';
import { rm } from 'node:fs/promises';

import { runChainlit } from './cypress/support/run';

export const CHAINLIT_APP_PORT = 8000;

// Chainlit removes FILES_DIRECTORY in its lifespan shutdown, after the port is
// already closed. Wait for the process to exit so the next spec cannot start
// inside that window and have its `.files` directory deleted by the old app.
const CHAINLIT_EXIT_TIMEOUT_MS = 5_000;

type ChainlitProcess = Awaited<ReturnType<typeof runChainlit>>;

let chainlitProcess: ChainlitProcess | undefined;

async function waitForExit(chainlit: ChainlitProcess | undefined) {
  if (!chainlit || chainlit.exitCode !== null || chainlit.signalCode !== null) {
    return true;
  }

  return new Promise<boolean>((resolve) => {
    const timeout = setTimeout(() => resolve(false), CHAINLIT_EXIT_TIMEOUT_MS);
    chainlit.once('exit', () => {
      clearTimeout(timeout);
      resolve(true);
    });
  });
}

async function forceKill(chainlit: ChainlitProcess) {
  if (chainlit.pid === undefined) {
    return;
  }

  if (process.platform === 'win32') {
    const { default: fkill } = await import('fkill');
    await fkill(chainlit.pid, { force: true, tree: true, silent: true });
    return;
  }

  try {
    process.kill(-chainlit.pid, 'SIGKILL'); // the process group spawned by run.ts
  } catch {
    // Already gone.
  }
}

async function killChainlit() {
  const chainlit = chainlitProcess;
  chainlitProcess = undefined;

  if (chainlit) {
    chainlit.kill('SIGTERM');

    if (!(await waitForExit(chainlit))) {
      // Shutdown can wait indefinitely for in-flight connections, e.g. a
      // socket.io long-poll. Kill the app before the next spec starts so it
      // cannot rmtree `.files` from under the new process.
      await forceKill(chainlit);
      await waitForExit(chainlit);
    }
  }

  // Fallback for an app that is not tracked here but still holds the port.
  const { default: fkill } = await import('fkill');
  await fkill(`:${CHAINLIT_APP_PORT}`, {
    force: true,
    silent: true
  });
}

['SIGTERM', 'SIGINT', 'SIGHUP', 'SIGBREAK'].forEach((signal) => {
  process.on(signal, () => {
    (async () => {
      await killChainlit(); // Ensure Chainlit is killed on exit

      const signalMap = { SIGTERM: 15, SIGINT: 2, SIGHUP: 1, SIGBREAK: 21 };
      process.exit(128 + (signalMap[signal] || 0));
    })();
  });
});

export default defineConfig({
  projectId: 'ij1tyk',

  retries: 3,

  viewportWidth: 1200,

  e2e: {
    defaultCommandTimeout: 30000,
    baseUrl: `http://127.0.0.1:${CHAINLIT_APP_PORT}`,
    experimentalInteractiveRunEvents: true,
    async setupNodeEvents(on, config) {
      cypressSplit(on, config);

      await killChainlit(); // Fallback to ensure no previous instance is running
      chainlitProcess = await runChainlit(); // Start bootstrap app before Cypress verifies baseUrl

      on('before:spec', async (spec) => {
        await killChainlit();
        chainlitProcess = await runChainlit(spec);
      });

      on('after:spec', async () => {
        await killChainlit();
      });

      on('after:run', async () => {
        await killChainlit();
      });

      on('task', {
        async deleteFileIfExists(filePath: string) {
          await rm(filePath, { force: true });
          return null;
        },
        log(message) {
          console.log(message);
          return null;
        },
        restartChainlit(spec: Cypress.Spec) {
          return new Promise((resolve) => {
            killChainlit().then(() => {
              runChainlit(spec).then((process) => {
                chainlitProcess = process;
                setTimeout(() => {
                  resolve(null);
                }, 1000);
              });
            });
          });
        }
      });

      return config;
    }
  }
});
