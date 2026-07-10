// Behavior-level harness for the endpoint-profile registry UI (SEC-P20 blocker 3).
//
// The browser runtime is unavailable, so this loads the ACTUAL inline script from
// demo-db/index.html into a minimal DOM + fetch shim (Node's vm) and drives the
// real activate/submit handlers. It proves the commit-status contract:
//   * a committed mutation whose post-commit registry refresh fails must NOT be
//     reported as a rejection (API/UI lifecycle agreement); and
//   * a successful save must remain described as committed (reset must not erase it);
//   * a genuine mutation rejection must still be reported as rejected (no over-correction).
// No external assets/storage/telemetry are introduced. Exit 0 iff every case holds.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(HERE, 'index.html'), 'utf8');
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) {
  console.error('NO_SCRIPT_FOUND in index.html');
  process.exit(2);
}
const scriptSrc = match[1];

// --- minimal DOM shim (no innerHTML sink is exercised; append/replaceChildren are inert) ---
function makeElement(id) {
  return {
    id: id || '', className: '', textContent: '', value: '', type: '', href: '', download: '',
    append() {}, replaceChildren() {}, appendChild() {}, addEventListener() {}, click() {},
  };
}
let elements = {};
const documentShim = {
  getElementById(id) {
    if (!elements[id]) elements[id] = makeElement(id);
    return elements[id];
  },
  createElement() { return makeElement(''); },
  createTextNode(text) { return { textContent: String(text) }; },
};

let currentFetch = async () => { throw new Error('fetch not configured'); };

const sandbox = {
  document: documentShim,
  fetch: (...args) => currentFetch(...args),
  URL: { createObjectURL: () => 'blob:stub', revokeObjectURL() {} },
  Blob: function Blob() {},
  console,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// Capture the real (closure-bound) handlers without altering any application logic.
const epilogue = `
;globalThis.__ui = {
  activateProfile, submitProfileForm, editProfile, supersedeProfile,
  status: () => ({ className: $('pf_status').className, text: $('pf_status').textContent }),
};
`;
vm.runInContext(scriptSrc + epilogue, sandbox, { filename: 'index.html#script' });
const ui = sandbox.__ui;

const RESULT = { profile: { id: 42, lifecycle_state: 'draft' }, evaluation: { decision: { verdict: 'TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED' } } };
const RESULT_UPDATE = { profile: { id: 5, lifecycle_state: 'draft' }, evaluation: { decision: { verdict: 'TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED' } } };
const PROFILE_DETAIL = {
  institution: { name: 'Synthetic Institution 5', bic: 'SYNBIC5', jurisdiction: 'EU synthetic profile' },
  legal_entity: { name: 'Synthetic Entity 5', lei: 'SYNTHLEI5', authority_status: 'current' },
  endpoint: {
    wallet_address: '0xSYN5', custody: 'Approved custodian', allowlist_status: 'current', endpoint_owner: 'Treasury ops queue',
    endpoint_payload_status: 'complete', requested_rail: 'Tokenized deposit', uetr: 'SYN-UETR-5',
    fallback_rail: 'Fiat SSI route', fallback_currency: 'EUR', fallback_account_mask: 'DE 4400', fallback_intermediary_bic: 'INTERDEFFXXX',
  },
};
const isDetailPath = (p) => /\/api\/endpoint-profiles\/\d+$/.test(p);
const resp = (ok, status, body) => ({ ok, status, json: async () => body });
const methodOf = (opt) => (opt && opt.method ? String(opt.method).toUpperCase() : 'GET');
const reset = () => { elements = {}; };

const cases = [
  {
    name: 'activate: commit succeeds but registry refresh fails -> NOT reported as rejected',
    async run() {
      reset();
      currentFetch = async (p, opt = {}) => {
        const m = methodOf(opt);
        if (m === 'POST' && p.includes('/activation')) return resp(true, 200, {});
        if (m === 'GET' && p.includes('/api/endpoint-profiles')) return resp(false, 503, { error: 'refresh_down' });
        throw new Error('unexpected ' + m + ' ' + p);
      };
      await ui.activateProfile(7);
      const s = ui.status();
      const ok = s.className !== 'pill bad' && !/rejected/i.test(s.text) && /activated profile #7/i.test(s.text) && /refresh/i.test(s.text);
      return { ok, s };
    },
  },
  {
    name: 'submit(create): success message must survive reset (not overwritten)',
    async run() {
      reset();
      currentFetch = async (p, opt = {}) => {
        const m = methodOf(opt);
        if (m === 'POST' && p === '/api/endpoint-profiles') return resp(true, 201, RESULT);
        if (m === 'GET' && p.includes('/api/endpoint-profiles')) return resp(true, 200, { endpoint_profiles: [] });
        throw new Error('unexpected ' + m + ' ' + p);
      };
      await ui.submitProfileForm({ preventDefault() {} });
      const s = ui.status();
      const ok = s.className === 'pill good' && /saved profile #42/i.test(s.text);
      return { ok, s };
    },
  },
  {
    name: 'submit(create): commit succeeds but refresh fails -> committed, distinct refresh warning',
    async run() {
      reset();
      currentFetch = async (p, opt = {}) => {
        const m = methodOf(opt);
        if (m === 'POST' && p === '/api/endpoint-profiles') return resp(true, 201, RESULT);
        if (m === 'GET') return resp(false, 503, { error: 'refresh_down' });
        throw new Error('unexpected ' + m + ' ' + p);
      };
      await ui.submitProfileForm({ preventDefault() {} });
      const s = ui.status();
      const ok = s.className !== 'pill bad' && !/rejected/i.test(s.text) && /saved profile #42/i.test(s.text) && /refresh/i.test(s.text);
      return { ok, s };
    },
  },
  {
    name: 'guard: a genuine mutation rejection is still reported as rejected',
    async run() {
      reset();
      currentFetch = async (p, opt = {}) => {
        const m = methodOf(opt);
        if (m === 'POST' && p.includes('/activation')) return resp(false, 409, { error: 'invalid_transition' });
        throw new Error('unexpected ' + m + ' ' + p);
      };
      await ui.activateProfile(9);
      const s = ui.status();
      const ok = s.className === 'pill bad' && /rejected/i.test(s.text) && /invalid_transition/.test(s.text);
      return { ok, s };
    },
  },
  {
    name: 'submit(update): commit succeeds but refresh fails -> committed, distinct refresh warning',
    async run() {
      reset();
      currentFetch = async (p, opt = {}) => {
        const m = methodOf(opt);
        if (m === 'GET' && isDetailPath(p)) return resp(true, 200, PROFILE_DETAIL);   // editProfile fetch
        if (m === 'PUT' && isDetailPath(p)) return resp(true, 200, RESULT_UPDATE);     // update commit
        if (m === 'GET' && p === '/api/endpoint-profiles') return resp(false, 503, { error: 'refresh_down' });
        throw new Error('unexpected ' + m + ' ' + p);
      };
      await ui.editProfile(5);                                // real closure sets editingProfileId -> PUT branch
      await ui.submitProfileForm({ preventDefault() {} });
      const s = ui.status();
      const ok = s.className !== 'pill bad' && !/rejected/i.test(s.text) && /saved profile #5/i.test(s.text) && /refresh/i.test(s.text);
      return { ok, s };
    },
  },
  {
    name: 'supersede: commit succeeds but refresh fails -> committed, distinct refresh warning',
    async run() {
      reset();
      currentFetch = async (p, opt = {}) => {
        const m = methodOf(opt);
        if (m === 'POST' && /\/supersession$/.test(p)) return resp(true, 200, {});      // supersession commit
        if (m === 'GET' && p === '/api/endpoint-profiles') return resp(false, 503, { error: 'refresh_down' });
        throw new Error('unexpected ' + m + ' ' + p);
      };
      await ui.supersedeProfile(3, '4');                     // real closure
      const s = ui.status();
      const ok = s.className !== 'pill bad' && !/rejected/i.test(s.text) && /superseded #3 with #4/i.test(s.text) && /refresh/i.test(s.text);
      return { ok, s };
    },
  },
];

async function main() {
  await new Promise((r) => setTimeout(r, 0)); // let init() settle
  let failures = 0;
  for (const c of cases) {
    try {
      const { ok, s } = await c.run();
      console.log(`[${ok ? 'PASS' : 'FAIL'}] ${c.name} :: ${JSON.stringify(s)}`);
      if (!ok) failures += 1;
    } catch (err) {
      console.log(`[FAIL] ${c.name} :: threw ${err && err.message}`);
      failures += 1;
    }
  }
  if (failures === 0) {
    console.log('UI_COMMIT_STATUS PASS');
    process.exit(0);
  }
  console.log(`UI_COMMIT_STATUS FAIL (${failures}/${cases.length})`);
  process.exit(1);
}
main();
