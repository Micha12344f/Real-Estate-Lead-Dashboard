// Syntax-only validation: never execute application scripts or contact services.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let files = 0;
let inlineScripts = 0;
function check(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      if (!['node_modules', '.vercel'].includes(entry.name)) check(file);
    } else if (/\.(?:js|cjs)$/.test(entry.name)) {
      new vm.Script(fs.readFileSync(file, 'utf8'), { filename: file });
      files++;
    } else if (/\.html$/.test(entry.name)) {
      const html = fs.readFileSync(file, 'utf8');
      for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
        if (/\bsrc\s*=|\btype\s*=\s*["'](?:application\/ld\+json|application\/json)["']/i.test(match[1])) continue;
        if (match[2].trim()) {
          new vm.Script(match[2], { filename: file + ':inline-script' });
          inlineScripts++;
        }
      }
    }
  }
}
check(path.join(__dirname, '../frontend'));
check(__dirname);
console.log(`JavaScript syntax passed: ${files} JS/CJS files, ${inlineScripts} inline HTML scripts.`);
