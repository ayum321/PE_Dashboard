const fs = require("fs");
const path = require("path");
const source = path.resolve(__dirname, "..", "src");
const apiFile = path.join(source, "api", "dashboardApi.ts");
if (!fs.existsSync(apiFile)) throw new Error(`Missing API adapter: ${apiFile}`);
const text = fs.readFileSync(apiFile, "utf8");
for (const required of ["credentials", "include", "/api"]) {
  if (!text.includes(required))
    throw new Error(`API contract missing: ${required}`);
}
console.log("MFE API contract check passed.");
