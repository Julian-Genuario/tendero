// Rebuild with: cd vendor && npm ci && npm run build
import { build } from 'esbuild';
import { cpSync, mkdirSync, rmSync } from 'node:fs';

const sch = 'node_modules/@bryntum/scheduler-community';
const core = 'node_modules/@bryntum/core-community';
const out = '../tendero/static/vendor/bryntum';
rmSync(out, { recursive: true, force: true });
mkdirSync(`${out}/fontawesome/css`, { recursive: true });
await build({ entryPoints: ['entry.js'], bundle: true, minify: true, format: 'iife', target: 'es2020',
              outfile: `${out}/scheduler.bundle.js`, legalComments: 'none' });
cpSync(`${sch}/scheduler.css`, `${out}/scheduler.css`);
cpSync(`${sch}/svalbard-light.css`, `${out}/svalbard-light.css`);
cpSync(`${sch}/LICENSE`, `${out}/LICENSE`);
for (const f of ['fontawesome.css', 'solid.css']) cpSync(`${core}/fontawesome/css/${f}`, `${out}/fontawesome/css/${f}`);
cpSync(`${core}/fontawesome/webfonts/fa-solid-900.woff2`, `${out}/fontawesome/webfonts/fa-solid-900.woff2`);
cpSync(`${core}/fontawesome/webfonts/fa-solid-900.ttf`, `${out}/fontawesome/webfonts/fa-solid-900.ttf`);
console.log('built', out);
