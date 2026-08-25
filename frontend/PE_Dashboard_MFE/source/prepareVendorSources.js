const fs = require('fs');
const path = require('path');

// react-double-scrollbar 0.0.15 references a .map file that its published
// package does not contain. CRA's source-map loader warns on every dev start.
// Removing the comment does not change the library's runtime code.
const target = path.join(
  __dirname,
  'node_modules',
  'react-double-scrollbar',
  'dist',
  'DoubleScrollbar.js',
);
const sourceMapDirective = /\r?\n\/\/# sourceMappingURL=DoubleScrollbar\.js\.map\s*$/;

if (!fs.existsSync(target)) {
  console.log('Vendor preparation skipped: dependencies are not installed yet.');
  process.exit(0);
}

const original = fs.readFileSync(target, 'utf8');
const prepared = original.replace(sourceMapDirective, '\n');

if (prepared !== original) {
  fs.writeFileSync(target, prepared, 'utf8');
  console.log('Vendor preparation complete: removed unavailable react-double-scrollbar source-map reference.');
}
