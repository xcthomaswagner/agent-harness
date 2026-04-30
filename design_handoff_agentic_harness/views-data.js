// Data for Home / Traces / Autonomy / Learning / Trace / PR views
// Seeded from agent-harness runtime/client-profiles and docs

window.VIEWS_DATA = (function(){

  const profiles = [
    {
      id: 'sitecore-cms',
      name: 'Sitecore',
      sample: 'Sitecore • Helix',
      in_flight: 2,
      completed_24h: 5,
      fpa: 0.72,
      escape: 0.11,
      catch: 0.64,
      auto_merge: 0.41,
    },
    {
      id: 'salesforce-apex',
      name: 'Salesforce',
      sample: 'Apex • LWC',
      in_flight: 3,
      completed_24h: 8,
      fpa: 0.81,
      escape: 0.07,
      catch: 0.78,
      auto_merge: 0.62,
    },
    {
      id: 'terraform-aws',
      name: 'Terraform',
      sample: 'AWS • IaC',
      in_flight: 0,
      completed_24h: 3,
      fpa: 0.88,
      escape: 0.04,
      catch: 0.85,
      auto_merge: 0.73,
    },
    {
      id: 'python-service',
      name: 'Python svc',
      sample: 'FastAPI • pytest',
      in_flight: 1,
      completed_24h: 6,
      fpa: 0.79,
      escape: 0.09,
      catch: 0.71,
      auto_merge: 0.58,
    },
  ];

  const traces = [
    { id: 'HARN-2043', title: 'Rollback-safe deploy on blue/green drift', profile: 'Salesforce', status: 'in-flight', phase: 'implementing', elapsed: '00:08:24', author: 'alex.tran', started: '14:02', live: true },
    { id: 'HARN-2041', title: 'Renewal pricing mismatch in Opportunity trigger', profile: 'Salesforce', status: 'in-flight', phase: 'planning',     elapsed: '00:02:11', author: 'jmurphy',   started: '14:18' },
    { id: 'HARN-2039', title: 'PDP hero layout breaks on RTL locales',           profile: 'Sitecore',   status: 'stuck',     phase: 'blocked',      elapsed: '01:42:06', author: 'r.okafor',  started: '12:38' },
    { id: 'HARN-2038', title: 'CI flake: marketplace.spec.ts ×7 retries',        profile: 'Python svc', status: 'stuck',     phase: 'reviewing',    elapsed: '00:54:12', author: 'system',    started: '13:27' },
    { id: 'HARN-2037', title: 'VPC peering module — tag propagation gap',        profile: 'Terraform',  status: 'queued',    phase: 'queued',       elapsed: '—',         author: 'd.chen',    started: '14:20' },
    { id: 'HARN-2034', title: 'Batch job retries loop on 429 from provider',     profile: 'Python svc', status: 'in-flight', phase: 'reviewing',    elapsed: '00:23:40', author: 'k.patel',   started: '13:58' },
    { id: 'HARN-2031', title: 'Auth callback race on slow upstreams',            profile: 'Python svc', status: 'done',      phase: 'merged',       elapsed: '01:12:44', author: 'l.yang',    started: '12:03' },
    { id: 'HARN-2028', title: 'Sitecore: personalisation rules not evaluating',  profile: 'Sitecore',   status: 'done',      phase: 'merged',       elapsed: '00:47:02', author: 'n.blake',   started: '11:17' },
  ];

  // ---- Lessons (Learning) ----
  const lessons = [
    { id: 'L-0142', state: 'proposed', title: 'Use connection pool on long-running Apex jobs', profile: 'Salesforce', source_trace: 'HARN-2043', evidence: 2, confidence: 0.62, created: '14:08', body: 'Detected three consecutive traces where transactional governor limits tripped after iteration 400. Suggest enforcing Database.Stateful on long batches and caching sObject lookups.' },
    { id: 'L-0141', state: 'proposed', title: 'Rename ambiguous `status` enum in WorkflowEngine', profile: 'Python svc', source_trace: 'HARN-2034', evidence: 4, confidence: 0.71, created: '13:41', body: '`status` collides with ORM field — 4 distinct runs misrouted retries. Lesson would route to naming rubric.' },
    { id: 'L-0138', state: 'draft', title: 'Always shim RTL direction in Sitecore PDP templates', profile: 'Sitecore', source_trace: 'HARN-2039', evidence: 6, confidence: 0.83, created: '12:48', body: 'Hero components assume LTR. Add direction prop to layout renderer and update Helix module tests.' },
    { id: 'L-0133', state: 'approved', title: 'Backoff on 429 must be respectful of Retry-After', profile: 'Python svc', source_trace: 'HARN-2034', evidence: 9, confidence: 0.91, created: '11:02' },
    { id: 'L-0129', state: 'applied', title: 'Prefer idempotency keys for provider webhook retries', profile: 'Python svc', source_trace: 'HARN-1988', evidence: 11, confidence: 0.94, created: 'Apr 18' },
    { id: 'L-0127', state: 'applied', title: 'Tag propagation in aws_vpc_peering modules', profile: 'Terraform', source_trace: 'HARN-1972', evidence: 7, confidence: 0.88, created: 'Apr 17' },
    { id: 'L-0120', state: 'snoozed', title: 'Consider decomposing large Opportunity trigger', profile: 'Salesforce', source_trace: 'HARN-1901', evidence: 3, confidence: 0.55, created: 'Apr 15' },
    { id: 'L-0118', state: 'rejected', title: 'Skip CI on docs-only changes', profile: 'Python svc', source_trace: 'HARN-1884', evidence: 2, confidence: 0.42, created: 'Apr 14' },
  ];

  // ---- Escaped defects (Autonomy) ----
  const escaped = [
    { id: 'D-2031-a', trace: 'HARN-2031', severity: 'minor', where: 'auth/callback_test.py', caught_in: 'staging',  note: 'Rare timing on cold-start; retried pass.' },
    { id: 'D-2028-b', trace: 'HARN-2028', severity: 'minor', where: 'PDP.personalize.tsx', caught_in: 'uat',       note: 'Locale fallback fired once.' },
    { id: 'D-1988-c', trace: 'HARN-1988', severity: 'major', where: 'providers/stripe.py', caught_in: 'production', note: 'Idempotency key missing on PUT — now covered by L-0129.' },
  ];

  // ---- Unmatched issues (Learning triage queue) ----
  const unmatched = [
    { id: 'U-2043-1', trace: 'HARN-2043', signal: 'pyright: implicit-Any at line 88', matches: 0, note: 'No lesson rule matches this site.' },
    { id: 'U-2041-1', trace: 'HARN-2041', signal: 'apex: System.LimitException (100 SOQL)', matches: 1, note: 'Weak match (0.31) to L-0120' },
    { id: 'U-2039-1', trace: 'HARN-2039', signal: 'playwright snapshot diff — hero position', matches: 0, note: 'Novel — suggest RTL shim lesson.' },
  ];

  // ---- Tickets-by-type (Autonomy) ----
  const byType = [
    { type: 'bug',     volume: 24, fpa: 0.81, escape: 0.06, auto_merge: 0.64 },
    { type: 'feature', volume: 11, fpa: 0.63, escape: 0.12, auto_merge: 0.37 },
    { type: 'chore',   volume: 18, fpa: 0.92, escape: 0.02, auto_merge: 0.88 },
    { type: 'spike',   volume: 4,  fpa: 0.44, escape: 0.31, auto_merge: 0.00 },
  ];

  // ---- Trace detail (deep) for HARN-2043 ----
  const traceDetail = {
    id: 'HARN-2043',
    title: 'Rollback-safe deploy on blue/green drift',
    profile: 'Salesforce',
    profile_id: 'salesforce-apex',
    author: 'alex.tran',
    branch: 'harness/HARN-2043',
    pr: 'PR #1184',
    started: 'Apr 19 · 14:02',
    elapsed: '00:08:24',
    status: 'in-flight',
    phases: [
      { key: 'planning',     name: 'Planning',      state: 'done',    dur: '1m 02s', tool: 'discuss.py',   events: 14 },
      { key: 'scaffolding',  name: 'Scaffolding',   state: 'done',    dur: '0m 48s', tool: 'worktree.sh',  events: 9 },
      { key: 'implementing', name: 'Implementing',  state: 'active',  dur: '5m 18s', tool: 'spawn_team',   events: 47 },
      { key: 'reviewing',    name: 'Reviewing',     state: 'pending', dur: '—',      tool: 'l3_pr_review', events: 0 },
      { key: 'merging',      name: 'Merging',       state: 'pending', dur: '—',      tool: 'ado-webhook',  events: 0 },
    ],
    sessions: [
      { role: 'PM',    agent: 'alex.tran',  state: 'done',    last: 'Planning notes published', at: '14:03' },
      { role: 'Dev',   agent: 'dev-01',     state: 'running', last: 'Patching DeployController.cls', at: '14:09' },
      { role: 'Dev',   agent: 'dev-02',     state: 'running', last: 'Adding @isTest for rollback path', at: '14:10' },
      { role: 'QA',    agent: 'qa-01',      state: 'idle',    last: 'Awaiting review handoff', at: '—' },
      { role: 'Lead',  agent: 'reviewer-0', state: 'idle',    last: 'Queued for L3 review', at: '—' },
    ],
    events: [
      { t: '14:02:04', ev: 'run.started',    p: 'planning',     msg: 'discuss.py spawned — scope 3 files, 1 trigger' },
      { t: '14:02:38', ev: 'plan.drafted',   p: 'planning',     msg: 'Plan committed to worktree' },
      { t: '14:03:06', ev: 'worktree.ready', p: 'scaffolding',  msg: 'Branch harness/HARN-2043 created from main@9a2f' },
      { t: '14:03:52', ev: 'team.spawn',     p: 'implementing', msg: 'dev-01, dev-02 spawned (parallel=2)' },
      { t: '14:05:12', ev: 'tool.edit',      p: 'implementing', msg: 'DeployController.cls — 42 lines changed' },
      { t: '14:06:40', ev: 'tool.test',      p: 'implementing', msg: 'apex tests — 28/28 passing locally' },
      { t: '14:07:18', ev: 'lesson.hit',     p: 'implementing', msg: 'L-0133 applied — retry-after respected' },
      { t: '14:08:02', ev: 'tool.edit',      p: 'implementing', msg: 'RollbackSafeDeploy_Test.cls — 66 lines added' },
      { t: '14:08:24', ev: 'heartbeat',      p: 'implementing', msg: 'dev-01 thinking… (commit candidate ready)' },
    ]
  };

  // ---- PR drilldown for PR #1184 ----
  const prDetail = {
    id: 'PR-1184',
    title: 'HARN-2043 · Rollback-safe deploy on blue/green drift',
    trace: 'HARN-2043',
    profile: 'Salesforce',
    author: 'alex.tran',
    branch: 'harness/HARN-2043',
    target: 'main',
    commits: 4,
    files: 6,
    additions: 184,
    deletions: 41,
    created: '14:07',
    status: 'review',
    checks: [
      { name: 'apex-unit',       state: 'pass', dur: '2m 04s' },
      { name: 'lint',            state: 'pass', dur: '0m 22s' },
      { name: 'sf-security',     state: 'warn', dur: '0m 48s', note: '1 minor finding' },
      { name: 'playwright-smoke',state: 'pending', dur: '—', note: 'waiting for sandbox' },
    ],
    issues: [
      { id: 'I-1', severity: 'major', where: 'force-app/main/default/classes/DeployController.cls:88', note: 'Unbounded SOQL on cold path — lesson match L-0120 (0.74)', matched: 'L-0120' },
      { id: 'I-2', severity: 'minor', where: 'force-app/main/default/classes/RollbackSafeDeploy_Test.cls:12', note: 'Assertion lacks message arg', matched: null },
      { id: 'I-3', severity: 'minor', where: 'manifest/package.xml', note: 'Formatting drift', matched: null },
    ],
    matches: [
      { lesson: 'L-0133', confidence: 0.91, applied: true, name: 'Backoff on 429 must respect Retry-After' },
      { lesson: 'L-0120', confidence: 0.74, applied: false, name: 'Consider decomposing large triggers' },
    ],
    auto_merge: {
      decision: 'hold',
      reasons: [
        'sf-security warn (1 finding)',
        'playwright-smoke pending',
        'L-0120 suggests refactor — human review recommended',
      ],
      confidence: 0.43
    }
  };

  return { profiles, traces, lessons, escaped, unmatched, byType, traceDetail, prDetail };
})();
