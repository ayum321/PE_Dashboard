/*
 * Static guard for the deployed React MFE boundary.  This is deliberately
 * dependency-free so it is safe in CI before the browser bundle is produced.
 */
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const src = path.join(root, 'src');
const adapter = path.join(src, 'api', 'dashboardApi.ts');
const index = path.join(src, 'index.tsx');
const failures = [];

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(full);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) ? [full] : [];
  });
}

function relative(file) { return path.relative(root, file).replace(/\\/g, '/'); }

for (const file of sourceFiles(src)) {
  const text = fs.readFileSync(file, 'utf8');
  if (file !== adapter && /\b(fetch\s*\(|new\s+XMLHttpRequest\s*\(|axios\s*\.)/.test(text)) {
    failures.push(`${relative(file)} bypasses src/api/dashboardApi.ts for an API call.`);
  }
  if (file !== adapter && /(?:localhost|127\.0\.0\.1|REACT_APP_)/.test(text)) {
    failures.push(`${relative(file)} contains local/runtime API configuration outside the approved adapter.`);
  }
}

const adapterText = fs.readFileSync(adapter, 'utf8');
const indexText = fs.readFileSync(index, 'utf8');
if (!/window\.env\.API_BASE_URL/.test(adapterText)) failures.push('dashboardApi.ts must obtain the API endpoint from window.env.API_BASE_URL.');
const requestBlock = adapterText.match(/const request\s*=\s*async[\s\S]*?\n};/);
const exportBlock = adapterText.match(/export const exportReportWithStatus[\s\S]*?\n};/);
if (!requestBlock || !/credentials\s*:\s*['\"]include['\"]/.test(requestBlock[0])) {
  failures.push('dashboardApi.ts shared request() must preserve credentials: include for pe_sid.');
}
if (!exportBlock || !/credentials\s*:\s*['\"]include['\"]/.test(exportBlock[0])) {
  failures.push('dashboardApi.ts exportReportWithStatus() must preserve credentials: include for pe_sid.');
}
if (!/xhr\.withCredentials\s*=\s*true/.test(adapterText)) failures.push('dashboardApi.ts upload XHR must preserve xhr.withCredentials = true.');
if (!/window\.env\.FRAME_URL_PATH/.test(indexText)) failures.push('index.tsx must derive the portal basename from window.env.FRAME_URL_PATH.');

if (failures.length) {
  console.error('MFE API contract check failed:');
  failures.forEach((failure) => console.error(`- ${failure}`));
  process.exit(1);
}

console.log('MFE API contract check passed.');
