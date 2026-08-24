const { exec } = require('child_process');
const readDirFiles = require('read-dir-files');
const decompress = require('decompress');
const shell = require('shelljs');
const editJsonFile = require('edit-json-file');
const chalk = require('chalk');

function updatePortalDependencies() {
  console.log(chalk.green('Starting dependencies update'));
  console.log(chalk.blackBright('- Fetching latest create-mfe package'));
  exec('npm pack @jda/cra-template-luminate-create-mfe', (err, stdout, stderr) => {
    if (err) {
      console.error(err);
    } else {
      console.log(chalk.blackBright('- Locate mfe-package'));
      readDirFiles.list('./', function (err, filenames) {
        if (err) return console.dir(err);
        console.log(chalk.blackBright('- Get mfe-package'));
        const matches = filenames.filter((s) => s.includes('jda-cra-template-luminate-create-mfe'));
        console.log(chalk.blackBright('- Move mfe-package'));
        shell.mv('./' + matches[0], './.github/scripts/' + matches[0]);
        console.log(chalk.blackBright('- Unpack mfe-package'));
        decompress('./.github/scripts/' + matches[0], './.github/scripts/temp', {
          map: (file) => {
            file.path = `${file.path}`;
            return file;
          },
        }).then(() => {
          console.log(chalk.blackBright('- Remove create-mfe-package'));
          shell.rm('-rf', __dirname + '/../../.github/scripts/' + matches[0]);
          console.log(chalk.blackBright('- Get data from template.json'));
          let templateJSON = editJsonFile(`./.github/scripts/temp/package/template.json`);
          console.log(chalk.blackBright('- Get data from main package.json'));
          let packageJSON = editJsonFile(__dirname + `/../../package.json`, { autosave: true });
          console.log(chalk.cyanBright('- Updating dependencies'));
          let templateDependencies = templateJSON.get('package.dependencies');
          Object.entries(packageJSON.get('dependencies')).forEach(([key]) => {
            if (templateDependencies.hasOwnProperty(key)) {
              const templateValue = templateDependencies[key];
              console.log(chalk.blackBright('- Updating ' + key + ' to ' + templateValue));
              packageJSON.set('dependencies.' + key, templateValue);
            }
          });
          console.log(chalk.cyanBright('- Updating dev dependencies'));
          let templateDevDependencies = templateJSON.get('package.devDependencies');
          Object.entries(packageJSON.get('devDependencies')).forEach(([key]) => {
            if (templateDevDependencies.hasOwnProperty(key)) {
              const templateValue = templateDevDependencies[key];
              console.log(chalk.blackBright('- Updating ' + key + ' to ' + templateValue));
              packageJSON.set('devDependencies.' + key, templateValue);
            }
          });
          console.log(chalk.cyanBright('- Updating resolutions'));
          let templateResolutions = templateJSON.get('package.resolutions');
          if (templateResolutions) {
            Object.entries(packageJSON.get('resolutions')).forEach(([key]) => {
              if (templateResolutions.hasOwnProperty(key)) {
                const templateValue = templateResolutions[key];
                console.log(chalk.blackBright('- Updating ' + key + ' to ' + templateValue));
                packageJSON.set('resolutions.' + key, templateValue);
              }
            });
          }

          console.log(chalk.cyanBright('- Remove temporal folder'));
          shell.rm('-rf', __dirname + '/../../.github/scripts/temp');
          console.log(chalk.cyanBright('- Running npm install'));
          exec('npm install', (err, stdout, stderr) => {
            if (err) {
              console.error(err);
            } else {
              console.log(stdout);
              console.log(chalk.green('Dependencies have been sucessfully updated'));
            }
          });
        });
      });
    }
  });
}

updatePortalDependencies();
