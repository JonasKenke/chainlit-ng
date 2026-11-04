import { ChildProcessWithoutNullStreams, spawn } from 'child_process';
import { access } from 'fs/promises';
import { dirname, join } from 'path';

export const runChainlit = async (
  spec: Cypress.Spec | null = null
): Promise<ChildProcessWithoutNullStreams> => {
  const chainlitDir = join(process.cwd(), 'backend', 'chainlit');
  // Cypress checks baseUrl before before:spec runs; use a current test app for that bootstrap.
  const bootstrapDir = join(process.cwd(), 'cypress', 'e2e', 'modes');
  const testDir = spec ? dirname(spec.absolute) : bootstrapDir;
  const entryPointFileName = spec
    ? spec.name.startsWith('async')
      ? 'main_async.py'
      : spec.name.startsWith('sync')
        ? 'main_sync.py'
        : 'main.py'
    : 'main.py';
  const entryPointPath = join(testDir, entryPointFileName);

  try {
    await access(entryPointPath);
  } catch {
    throw new Error(`Entry point file does not exist: ${entryPointPath}`);
  }

  return new Promise((resolve, reject) => {
    const chainlit = spawn(
      'uv',
      [
        '--project',
        chainlitDir,
        'run',
        'chainlit',
        'run',
        entryPointPath,
        '-h',
        '--ci'
      ],
      {
        env: {
          ...process.env,
          CHAINLIT_APP_ROOT: testDir
        }
      }
    );

    chainlit.stdout.on('data', (data) => {
      if (data.toString().includes('Your app is available at')) {
        resolve(chainlit);
      }
    });

    chainlit.stderr.on('data', (data) => {
      console.error(`[Chainlit stderr] ${data}`);
    });

    chainlit.on('error', (error) => reject(error));
    chainlit.on('exit', (code) => {
      reject(new Error(`Chainlit process exited with code ${code}`));
    });
  });
};
