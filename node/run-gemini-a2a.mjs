import { readFileSync } from 'node:fs';
import { createRequire, registerHooks } from 'node:module';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { EnvHttpProxyAgent, setGlobalDispatcher } from 'undici';

const require = createRequire(import.meta.url);
const manifestPath = require.resolve('@google/gemini-cli-a2a-server/package.json');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
if (manifest.version !== '0.68.0') {
  throw new Error('This launcher requires Gemini CLI A2A server 0.68.0.');
}
const serverPath = resolve(dirname(manifestPath), manifest.bin['gemini-cli-a2a-server']);
const serverUrl = pathToFileURL(serverPath).href;

if (!process.env.GEMINI_API_KEY && !process.env.USE_CCPA) {
  const { loadApiKey } = await import('@google/gemini-cli-core');
  const key = await loadApiKey();
  if (key) {
    process.env.GEMINI_API_KEY = key;
  }
}

const exclude = process.env.no_proxy ?? process.env.NO_PROXY;
const noProxy = [exclude, '127.0.0.1', 'localhost'].filter(Boolean).join(',');
process.env.NO_PROXY = noProxy;
if (process.env.no_proxy !== undefined) {
  process.env.no_proxy = noProxy;
}

function replaceOnce(source, marker, replacement) {
  if (source.split(marker).length !== 2) {
    throw new Error('Gemini CLI A2A server 0.68.0 has an unexpected bundle shape.');
  }
  return source.replace(marker, () => replacement);
}

const model = process.env.AGENT_A2A_GEMINI_MODEL?.trim();
const hook = registerHooks({
  load(url, context, nextLoad) {
    const loaded = nextLoad(url, context);
    if (url !== serverUrl) {
      return loaded;
    }
    // Bind the official server to the numeric loopback address used by the bridge.
    let source = replaceOnce(
      Buffer.from(loaded.source).toString('utf8'),
      'expressApp.listen(port, "localhost",',
      'expressApp.listen(port, "127.0.0.1",',
    );
    // v0.68.0 has no public launch-time model setting.
    if (model) {
      source = replaceOnce(source, 'model: PREVIEW_GEMINI_MODEL,', `model: ${JSON.stringify(model)},`);
    }
    return { ...loaded, source };
  },
});

process.argv[1] = serverPath;
try {
  await import(serverUrl);
} finally {
  hook.deregister();
}

// The upstream bundle installs a direct dispatcher on import.
if (process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.https_proxy || process.env.http_proxy) {
  setGlobalDispatcher(new EnvHttpProxyAgent());
}
