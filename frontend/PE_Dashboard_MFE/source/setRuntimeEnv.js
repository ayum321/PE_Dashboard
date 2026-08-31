const path = require('path');
const ejs = require('ejs');
const fs = require('fs');

const sourceEnvFile = path.join(__dirname, 'envs', 'env.ejs');
const outputDirectory = process.argv[2];

async function writeRuntimeEnv() {
  if (!outputDirectory) {
    throw new Error('Output directory is required. Example: node setRuntimeEnv.js ./build/');
  }

  const destinationEnvFile = path.resolve(outputDirectory, 'env.js');
  // Local scripts historically used camel-case names while deployment systems
  // conventionally expose uppercase variables. Supporting both keeps env.js
  // portable without placing configuration in the compiled React bundle.
  const runtimeData = {
    ...process.env,
    appName: process.env.appName || process.env.LOCAL_APP_NAME || 'PE Audit Dashboard',
    frameUrlPath: process.env.frameUrlPath || process.env.FRAME_URL_PATH || '/',
    apiBaseUrl: process.env.apiBaseUrl || process.env.API_BASE_URL || '',
  };
  const envFile = await ejs.renderFile(sourceEnvFile, { data: runtimeData }, { async: true });
  await fs.promises.mkdir(path.dirname(destinationEnvFile), { recursive: true });
  await fs.promises.writeFile(destinationEnvFile, envFile, 'utf8');
  console.log(`Runtime environment ready: ${path.relative(process.cwd(), destinationEnvFile)}`);
}

writeRuntimeEnv().catch((error) => {
  console.error(`Unable to generate runtime environment: ${error.message}`);
  process.exitCode = 1;
});
