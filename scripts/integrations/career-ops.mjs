// A narrow bridge to Career Ops' public HTTP providers. No agent workflows,
// local-parser commands, browser installs, profile files or application actions.
import { pathToFileURL } from 'node:url';
import path from 'node:path';
const root = process.argv[2];
const input = JSON.parse(await new Promise(resolve => {
  let data = ''; process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => data += chunk);
  process.stdin.on('end', () => resolve(data));
}));
const supported = ['greenhouse', 'ashby', 'lever', 'workable', 'smartrecruiters', 'recruitee', 'oraclecloud'];
const providers = await Promise.all(supported.map(async id =>
  (await import(pathToFileURL(path.join(root, 'providers', `${id}.mjs`)))).default));
const { makeHttpCtx } = await import(pathToFileURL(path.join(root, 'providers/_http.mjs')));
const entries = input.entries.map(entry => {
  const provider = providers.find(p => {
    if (entry.provider && entry.provider !== p.id) return false;
    try { return !!p.detect(entry); } catch { return false; }
  });
  return provider ? { ...entry, provider: provider.id } : null;
}).filter(Boolean);
if (input.catalog) {
  process.stdout.write(JSON.stringify(entries));
} else {
  // Emit each completed company separately: one timed-out board cannot erase
  // the useful results from boards that already finished.
  const pending = [...entries];
  await Promise.all(Array.from({length: Math.min(4, pending.length)}, async () => {
    while (pending.length) {
      const entry = pending.shift();
      try {
        const jobs = await providers.find(p => p.id === entry.provider).fetch(entry, makeHttpCtx());
        process.stdout.write(JSON.stringify({company: entry.name, provider: entry.provider, jobs}) + '\n');
      } catch (error) {
        process.stdout.write(JSON.stringify({company: entry.name, error: error.message}) + '\n');
      }
    }
  }));
}
