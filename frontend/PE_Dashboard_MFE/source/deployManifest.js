const https = require('https');

const key = process.env[`dashboardMfeRegistrationApiKey`];

const registrationUrl = process.env[`portalRegistrationServiceUrl`];
const frameUrl = process.env[`dashboardMfeFrameUrl`];
const appId = process.env[`dashboardMfeRegistrationAppId`];
const scopes = process.env[`msalScpScope`].split(',');

const currentManifest = require('../../manifest.json');

try {
  currentManifest.frameUrl = frameUrl;
  currentManifest.id = appId;
  currentManifest.scopes = scopes;
  const request = https.request(
    registrationUrl,
    {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
      },
    },
    (res) => {
      console.log(res.statusCode);
      if (res.statusCode === 200) {
        process.exit(0);
      } else {
        let responseString = '';
        res.on('data', (data) => {
          responseString += data;
        });
        res.on('end', () => {
          console.error(responseString);
          process.exit(1);
        });
      }
    },
  );
  request.write(
    JSON.stringify({
      apiKey: key,
      app: currentManifest,
    }),
  );
  request.end();
} catch (e) {
  console.error(e);
  process.exit(1);
}
