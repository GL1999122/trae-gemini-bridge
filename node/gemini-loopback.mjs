import { Agent } from 'undici';

const origin = process.env.AGENT_A2A_TRAE_ORIGIN;
const token = process.env.AGENT_A2A_SESSION_TOKEN;
if (!origin || !/^http:\/\/127\.0\.0\.1:[0-9]+$/.test(origin) || new URL(origin).origin !== origin || !token) {
  throw new Error('Gemini A2A requires a numeric loopback TRAE origin and an active session token.');
}

// Run the official CLI in this process so its supervisor does not reload this
// preload in a child after the one-time token has been removed.
process.env.GEMINI_CLI_NO_RELAUNCH = 'true';
delete process.env.AGENT_A2A_SESSION_TOKEN;

const direct = new Agent({ headersTimeout: 180000, bodyTimeout: 180000 });
const fetch = globalThis.fetch;
globalThis.fetch = (input, options) => {
  const address = input instanceof Request ? input.url : input;
  if (URL.canParse(address) && new URL(address).origin === origin) {
    const headers = new Headers(options?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set('Authorization', `Bearer ${token}`);
    return fetch(input, { ...options, headers, dispatcher: direct, redirect: 'error' });
  }
  return fetch(input, options);
};
