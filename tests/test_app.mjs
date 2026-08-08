// Characterization tests for apps/jobsearch-os/ui/app.js
//
// app.js is a browser script (not a module): it defines top-level consts and
// functions, assigns to `window`, and calls init() at the bottom. We load it
// via `new Function` with stubbed browser globals, strip the auto-init call,
// and append an export object so its pure functions become testable in Node.
//
// The render functions are locked with golden snapshots captured from the
// current behavior — any refactor that changes output by one character fails.
//
// Run:  node tests/test_app.mjs

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const APP_JS = path.resolve(__dirname, '../apps/jobsearch-os/ui/app.js');
const SNAP_DIR = path.join(__dirname, '__snapshots__');
const SNAP_FILE = path.join(SNAP_DIR, 'app-snapshots.json');

// ── Load app.js into a sandbox and capture its pure functions ──────────────────
function loadApp() {
  let src = fs.readFileSync(APP_JS, 'utf8');
  // Strip the trailing auto-run so loading doesn't touch the (absent) DOM.
  src = src.replace(/\ninit\(\);\s*$/, '\n');
  // Names we want to pull out for testing (all top-level fn declarations / consts).
  const EXPORTS = [
    'state', 'SECTION_META', 'emptyProfile', 'hasProfileData', 'mergeProfile',
    'parseAgentJson', 'scoreColorFor', 'escHtml', 'escAttr',
    'isTextFile', 'renderJD', 'renderSection', 'renderSectionView',
    'renderSectionEdit', 'renderResumeHTML', 'buildExportHTML', 'matchChip',
    'matchChips', 'renderFormCard',
  ];
  src += `\n;module.exports = { ${EXPORTS.join(', ')} };\n`;
  const factory = new Function('module', 'exports', 'window', src);
  const mod = { exports: {} };
  factory(mod, mod.exports, /* window */ {});
  return mod.exports;
}

const app = loadApp();

// ── Tiny test + snapshot runner (zero deps) ────────────────────────────────────
let snaps = {};
if (fs.existsSync(SNAP_FILE)) snaps = JSON.parse(fs.readFileSync(SNAP_FILE, 'utf8'));
const nextSnaps = {};
let pass = 0, fail = 0, created = 0;
const failures = [];

function test(name, fn) {
  try { fn(); pass++; }
  catch (e) { fail++; failures.push(`${name}: ${e.message}`); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || 'not equal'}\n  expected: ${JSON.stringify(b)}\n  actual:   ${JSON.stringify(a)}`);
}
// Snapshot: record on first run, compare thereafter.
function snapshot(key, value) {
  nextSnaps[key] = value;
  if (!(key in snaps)) { created++; return; }
  if (snaps[key] !== value) {
    throw new Error(`snapshot mismatch for "${key}"\n  --- expected ---\n${snaps[key]}\n  --- actual ---\n${value}`);
  }
}

// ── Fixtures ───────────────────────────────────────────────────────────────────
const PROFILE = {
  identity: { name: 'Ada <Lovelace>', headline: 'Engineer & "Analyst"', summary: 'Builds things.', location: 'London' },
  contact: { email: 'ada@example.com', phone: '+44 123', links: [{ label: 'GitHub', url: 'https://github.com/ada' }, { label: '', url: 'https://x.com/ada' }] },
  skill_buckets: [{ category: 'Languages', skills: ['Python', 'C++'] }, { category: 'Cloud', skills: ['AWS'] }],
  experience: [{ id: 'e1', title: 'Lead Dev', company: 'Acme', start: '2020', end: 'Present', raw_description: 'Did stuff.', highlights: ['Shipped X', 'Scaled Y'], tags: ['leadership'] }],
  projects: [{ id: 'p1', name: 'Engine', description: 'A thing', url: 'https://eng.dev', raw_description: 'Tech detail', tech: ['Rust'], highlights: ['Fast'], tags: ['oss'] }],
  education: [{ degree: 'BSc CS', institution: 'MIT', year: '2018' }],
  certifications: [{ name: 'AWS SAA', issuer: 'Amazon', year: '2021' }, 'Plain String Cert'],
  publications: [{ title: 'On Engines', venue: 'ACM', year: '2022', url: 'https://doi.org/x' }],
};
const MATCH = {
  match_score: 72, summary: 'Strong fit.', application_strategy: 'Lead with cloud.',
  skills_matched: ['Python', 'AWS'], partial_matches: [{ skill: 'GCP', reason: 'similar to AWS' }],
  required_gaps: ['Kubernetes'], nice_to_have_gaps: ['Terraform'],
  relevant_experience: 'Your Acme role maps well.',
  relevant_projects: [{ name: 'Engine', reason: 'shows systems depth', talking_points: ['perf work'] }],
  green_flags: ['Leadership'], focus_areas: ['Learn k8s'],
  apply_readiness: { verdict: 'stretch', reason: 'one gap' }, profile_gaps: ['Docker'],
};
const DRAFT = {
  summary: 'Targeted summary.', skills: ['Python', 'AWS', 'Docker'],
  experience: [{ id: 'e1', bullets: ['Tailored bullet 1', 'Tailored bullet 2'] }],
  projects: [{ id: 'p1', bullets: ['Project bullet'] }],
};

function withProfile(p, editing, fn) {
  const savedP = app.state.profile, savedE = app.state.editingSection;
  app.state.profile = p; app.state.editingSection = editing ?? null;
  try { return fn(); } finally { app.state.profile = savedP; app.state.editingSection = savedE; }
}

// ── Pure helpers ───────────────────────────────────────────────────────────────
test('escHtml escapes the five entities', () => {
  eq(app.escHtml(`<a href="x">&'`), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;');
  eq(app.escHtml(null), '');
  eq(app.escHtml(42), '42');
});

test('isTextFile matches only text extensions', () => {
  for (const n of ['a.txt', 'a.md', 'a.markdown', 'a.json', 'A.TXT']) assert(app.isTextFile(n), n);
  for (const n of ['a.pdf', 'a.docx', 'a', 'a.jsonx']) assert(!app.isTextFile(n), n);
});

test('scoreColorFor buckets at 75 and 50', () => {
  eq(app.scoreColorFor(90), 'var(--green)');
  eq(app.scoreColorFor(75), 'var(--green)');
  eq(app.scoreColorFor(74), 'var(--accent)');
  eq(app.scoreColorFor(50), 'var(--accent)');
  eq(app.scoreColorFor(49), 'var(--red)');
});

test('emptyProfile has all eight sections', () => {
  const p = app.emptyProfile();
  for (const k of ['identity', 'contact', 'skill_buckets', 'experience', 'projects', 'education', 'certifications', 'publications'])
    assert(k in p, `missing ${k}`);
});

test('hasProfileData detects content', () => {
  assert(!app.hasProfileData(null));
  assert(!app.hasProfileData(app.emptyProfile()));
  assert(app.hasProfileData({ identity: { name: 'x' } }));
  assert(app.hasProfileData({ experience: [{}] }));
});

// ── JSON parsers (the three dedup targets must behave identically) ──────────────
const JSON_CASES = [
  ['plain', '{"a":1}'],
  ['fenced', '```json\n{"a":1}\n```'],
  ['fenced-no-lang', '```\n{"a":1}\n```'],
  ['surrounded by prose', 'Here you go:\n{"a":1}\nThanks'],
  ['leading/trailing ws', '   {"a":1}   '],
];
for (const [label, input] of JSON_CASES) {
  test(`parseAgentJson handles ${label}`, () => eq(JSON.stringify(app.parseAgentJson(input)), '{"a":1}'));
}

// ── mergeProfile (structural — ids contain Date.now so check shape) ─────────────
test('mergeProfile preserves existing and appends extracted', () => {
  const existing = app.emptyProfile();
  existing.identity.name = 'Keep Me';
  existing.skill_buckets = [{ category: 'Languages', skills: ['Python'] }];
  const extracted = {
    identity: { headline: 'New Headline' },
    contact: { email: 'new@x.com', links: [{ label: 'GH', url: 'https://gh' }] },
    skill_buckets: [{ category: 'languages', skills: ['Python', 'Go'] }, { category: 'Cloud', skills: ['AWS'] }],
    experience: [{ title: 'Dev', company: 'Acme' }],
    projects: [{ name: 'Proj' }],
    education: [{ degree: 'BSc' }],
  };
  const m = app.mergeProfile(existing, extracted);
  eq(m.identity.name, 'Keep Me', 'kept existing name');
  eq(m.identity.headline, 'New Headline', 'merged headline');
  eq(m.contact.email, 'new@x.com');
  // case-insensitive bucket merge: Python kept once, Go appended
  const langs = m.skill_buckets.find(b => b.category.toLowerCase() === 'languages');
  eq(JSON.stringify(langs.skills), '["Python","Go"]');
  assert(m.skill_buckets.some(b => b.category === 'Cloud'), 'new bucket added');
  eq(m.experience.length, 1); assert(m.experience[0].id, 'experience got id');
  eq(m.projects.length, 1); assert(m.projects[0].id, 'project got id');
  eq(m.education.length, 1);
  // original input not mutated
  assert(existing.experience.length === 0, 'existing not mutated');
});

test('mergeProfile skips empty entries', () => {
  const m = app.mergeProfile(app.emptyProfile(), {
    experience: [{ title: '', company: '' }], projects: [{ name: '' }],
    skill_buckets: [{ category: '', skills: ['x'] }, { category: 'X', skills: [] }],
  });
  eq(m.experience.length, 0); eq(m.projects.length, 0); eq(m.skill_buckets.length, 0);
});

// ── Render snapshots ───────────────────────────────────────────────────────────
for (const name of Object.keys(app.SECTION_META)) {
  test(`renderSectionView snapshot: ${name}`, () =>
    withProfile(PROFILE, null, () => snapshot(`view:${name}`, app.renderSectionView(name))));
  test(`renderSectionEdit snapshot: ${name}`, () =>
    withProfile(PROFILE, name, () => snapshot(`edit:${name}`, app.renderSectionEdit(name))));
  test(`renderSectionView empty snapshot: ${name}`, () =>
    withProfile(app.emptyProfile(), null, () => snapshot(`view-empty:${name}`, app.renderSectionView(name))));
}
test('renderSection wraps view with header/actions', () =>
  withProfile(PROFILE, null, () => snapshot('section:identity', app.renderSection('identity'))));

test('renderResumeHTML snapshot', () =>
  withProfile(PROFILE, null, () => snapshot('resumeHTML', app.renderResumeHTML(DRAFT, PROFILE))));
test('buildExportHTML snapshot', () =>
  withProfile(PROFILE, null, () => snapshot('exportHTML', app.buildExportHTML(DRAFT, PROFILE))));

test('renderJD snapshot (headings, bullets, paras)', () =>
  snapshot('jd', app.renderJD('Responsibilities:\n- Build stuff\n- Ship it\n\nPlain paragraph here.')));
test('renderJD empty', () => assert(app.renderJD('').includes('No description')));

test('renderFormCard snapshot', () => snapshot('formCard', app.renderFormCard({
  name: 'Application', fields: [
    { typeName: 'Email', type: 'email', label: 'Your email', required: true, helperText: 'work email' },
    { typeName: 'File Upload', type: 'file', label: 'Resume', required: false, accept: '.pdf' },
  ],
})));

test('matchChips snapshot + empty', () => {
  snapshot('matchChips', app.matchChips(['Python', 'AWS'], 'var(--green)'));
  assert(app.matchChips([], 'x').includes('None identified'));
});

// ── Report ─────────────────────────────────────────────────────────────────────
if (created > 0) {
  fs.mkdirSync(SNAP_DIR, { recursive: true });
  fs.writeFileSync(SNAP_FILE, JSON.stringify(nextSnaps, null, 2));
}
console.log(`\nJS  ${pass} passed, ${fail} failed` + (created ? `, ${created} snapshots created` : ''));
for (const f of failures) console.log('  FAIL ' + f);
process.exit(fail ? 1 : 0);
