// Offline export integrity/privacy checks; never execute node code or call n8n.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const directory = path.join(__dirname, 'n8n');
const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'tve-lead-intelligence-manifest.json'), 'utf8'));
assert.equal(manifest.importsActivateByDefault, false);
assert.equal(manifest.workflowFiles.length, 12);
const placeholders = new Set(['tve-lead-intelligence-create_follow_up_task.json', 'tve-lead-intelligence-add_crm_note.json']);
let codeNodes = 0;

function inspect(value, location) {
  if (!value || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) {
    const label = `${location}/${key}`;
    assert.ok(!['pinData', 'staticData', 'executionData'].includes(key), `${label}: runtime data is not source`);
    if (key === 'credentials') {
      for (const [type, ref] of Object.entries(child)) {
        assert.deepEqual(Object.keys(ref).sort(), ['id', 'name'], `${label}: credential reference metadata only`);
        const known = manifest.credentials[ref.id];
        assert.ok(known && known.type === type && known.name === ref.name, `${label}: unknown credential reference`);
      }
      continue;
    }
    if (/(password|secret|api[_-]?key|authorization|access[_-]?token|private[_-]?key)/i.test(key) && typeof child === 'string') {
      assert.ok(!child || child.startsWith('='), `${label}: literal credential-like parameter`);
    }
    inspect(child, label);
  }
}

for (const entry of manifest.workflowFiles) {
  assert.equal(path.basename(entry.file), entry.file);
  const source = fs.readFileSync(path.join(directory, entry.file), 'utf8').replace(/\r\n/g, '\n');
  assert.equal(crypto.createHash('sha256').update(source).digest('hex'), entry.sha256, `${entry.file}: checksum mismatch`);
  const workflow = JSON.parse(source);
  assert.equal(workflow.name, entry.name);
  assert.notEqual(workflow.active, true, `${entry.file}: exports must not activate on import`);
  inspect(workflow, entry.file);
  const names = new Set(workflow.nodes.map(node => node.name));
  assert.equal(names.size, workflow.nodes.length, `${entry.file}: duplicate node names`);
  for (const node of workflow.nodes) {
    if (node.type === 'n8n-nodes-base.webhook') assert.equal(node.parameters.authentication, 'headerAuth');
    if (node.parameters.jsCode) {
      new vm.Script(`(function () {\n${node.parameters.jsCode}\n})`, { filename: `${entry.file}:${node.name}` });
      codeNodes++;
    }
  }
  for (const [name, outputs] of Object.entries(workflow.connections)) {
    assert.ok(names.has(name), `${entry.file}: missing connection source`);
    for (const branches of Object.values(outputs)) for (const branch of branches) for (const target of branch) {
      assert.ok(names.has(target.node), `${entry.file}: missing connection target`);
    }
  }
  if (placeholders.has(entry.file)) {
    assert.equal(entry.active, false);
    assert.equal(entry.functionalWriteImplementation, false);
    assert.ok(workflow.nodes.every(node => !['n8n-nodes-base.httpRequest', 'n8n-nodes-base.emailSend'].includes(node.type)));
  }
}
console.log(`n8n source checks passed: ${manifest.workflowFiles.length} workflows, ${codeNodes} Code nodes; credential references only.`);
