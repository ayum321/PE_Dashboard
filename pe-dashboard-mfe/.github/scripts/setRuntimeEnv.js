const path = require('path');
const ejs = require('ejs');
const fs = require('fs-extra');

const args = process.argv.slice(2);
const sourceEnvFile = path.join(__dirname, '..', 'envs', 'env.ejs');
console.log(`sourceEnvFile=${sourceEnvFile}`);
const destinationEnvFile = `${args[0]}env.js`;

async function writeRuntimeEnv() {
  try {
    await writeOutPutFile(destinationEnvFile, sourceEnvFile, process.env);
    process.exit(0);
  } catch (e) {
    console.error(e);
    process.exit(1);
  }
}

async function writeOutPutFile(fileName, templateFileName, environmentVariables) {
  console.log(`fileName=${fileName}`);
  if (!fileName) {
    console.error(`Output filename missing`);
  }
  if (!templateFileName) {
    console.error(`Output template missing`);
  }
  console.log(`About to write to file ${fileName} using template ${templateFileName}:\n`);
  const envFileBuffer = await ejs
    .renderFile(`${templateFileName}`, { data: environmentVariables }, { async: true })
    .catch((error) => {
      console.error(error);
    });
  try {
    fs.outputFileSync(fileName, envFileBuffer);
  } catch (ex) {
    console.error(ex);
  }
  console.log(`Writing to file ${fileName} using template ${templateFileName}:\nBuffer:\n${envFileBuffer}`);
}

writeRuntimeEnv();
