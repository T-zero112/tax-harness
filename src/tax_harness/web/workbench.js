const byId = (id) => document.getElementById(id);
const scopeForm = byId('scope');
const status = byId('status');
const memoryForm = byId('memory-form');
const ruleDialog = byId('rule-dialog');
const ruleForm = byId('rule-form');
const chatForm = byId('chat-form');
const sessions = new Map();
let scope = Object.fromEntries(new FormData(scopeForm));
let busy = false;
let chatAvailable = false;
let scopeAvailable = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, onClick) {
  const node = element('button', text, 'secondary');
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function entry(title, meta) {
  const row = element('article', undefined, 'entry');
  const header = element('div', undefined, 'entry-header');
  header.append(element('h3', title));
  row.append(header, element('p', meta, 'meta'));
  return { row, header };
}

async function request(path, { method = 'GET', body, query = {} } = {}) {
  const params = new URLSearchParams({ enterprise_id: scope.enterprise, month: scope.month, ...query });
  const response = await fetch(`${path}?${params}`, {
    method,
    headers: { 'X-Demo-User': scope.user, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const validation = Array.isArray(result.detail) ? result.detail.map((item) => item.msg).join('，') : result.detail;
    throw new Error(result.message || validation || `请求失败（${response.status}）`);
  }
  return result;
}

async function run(action, feedback = status) {
  if (busy) return;
  busy = true;
  feedback.textContent = '';
  const controls = Array.from(document.querySelectorAll('button:not(:disabled), #scope input, #scope select'));
  controls.forEach((control) => { control.disabled = true; });
  try {
    await action();
  } catch (error) {
    feedback.textContent = error.message;
  } finally {
    controls.forEach((control) => { control.disabled = false; });
    busy = false;
    byId('new-rule').disabled = !scopeAvailable || scope.user !== 'U001';
    memoryForm.querySelector('button').disabled = !scopeAvailable || !memoryForm.elements.source_message_id.value;
    chatForm.querySelector('button').disabled = !scopeAvailable || !chatAvailable;
  }
}

function renderRules(items, filtered = false) {
  const target = byId('rule-list');
  target.replaceChildren();
  if (!items.length) target.append(element('p', '当前范围内没有匹配的规则。', 'muted'));
  for (const item of items) {
    const rule = filtered ? item : item.rule;
    const active = filtered || item.active;
    const citation = rule.citation;
    const { row, header } = entry(citation.name, `${citation.doc_id} · v${citation.version} · ${rule.effective_from} 生效 · ${active ? '启用' : '已停用'}`);
    if (active && scope.user === 'U001') {
      header.append(button('停用版本', () => {
        if (!confirm(`停用 ${citation.doc_id} v${citation.version}？`)) return;
        run(async () => {
          await request(`/api/rules/${encodeURIComponent(citation.doc_id)}/deactivate`, {
            method: 'POST', query: { version: citation.version }, body: { expected_revision: rule.revision },
          });
          byId('rule-search').elements.query.value = '';
          renderRules(await request('/api/rules'));
          status.textContent = '规则版本已停用。';
        });
      }));
    }
    const details = element('details');
    details.append(element('summary', '查看正文'), element('p', rule.content));
    row.append(details);
    target.append(row);
  }
}

function renderMemories(items) {
  const target = byId('memory-list');
  target.replaceChildren();
  if (!items.length) target.append(element('p', '还没有保存的偏好。', 'muted'));
  for (const memory of items) {
    const { row, header } = entry(memory.content, `${memory.enterprise_id || '所有有权企业'} · 修订 ${memory.revision} · 来源 ${memory.source_message_id}`);
    const path = `/api/memories/${encodeURIComponent(memory.memory_id)}`;
    const actions = element('div', undefined, 'actions');
    actions.append(button('更正', () => {
      const content = prompt('更正偏好', memory.content);
      if (content === null) return;
      run(async () => {
        await request(path, { method: 'PATCH', body: { content, expected_revision: memory.revision } });
        renderMemories(await request('/api/memories'));
        status.textContent = '偏好已更正。';
      });
    }), button('删除', () => {
      if (!confirm('删除这条长期偏好？')) return;
      run(async () => {
        await request(path, { method: 'DELETE', query: { expected_revision: memory.revision } });
        renderMemories(await request('/api/memories'));
        status.textContent = '偏好已删除。';
      });
    }));
    header.append(actions);
    target.append(row);
  }
}

function renderRecords(records) {
  byId('invoice-count').textContent = records.invoice_count;
  byId('bank-count').textContent = records.bank_count;
  const options = memoryForm.elements.source_message_id;
  options.replaceChildren();
  const placeholder = element('option', records.source_messages.length ? '选择用户消息' : '当前范围暂无用户消息');
  placeholder.value = '';
  options.append(placeholder);
  for (const message of records.source_messages) {
    const option = element('option', `${message.message_id} · ${message.content.slice(0, 70)}`);
    option.value = message.message_id;
    options.append(option);
  }
  const target = byId('record-list');
  target.replaceChildren();
  for (const task of records.tasks) {
    const { row, header } = entry(`任务 ${task.task_id}`, `${task.status} · 修订 ${task.revision}`);
    if (!['completed', 'cancelled'].includes(task.status)) {
      header.append(button('更正待办', () => run(async () => {
        const path = `/api/tasks/${encodeURIComponent(task.task_id)}`;
        const current = await request(path);
        const question = prompt('输入新的待办，留空可清除。', current.question || '');
        if (question === null) return;
        await request(path, { method: 'PATCH', body: { question: question.trim() || null, expected_revision: current.revision } });
        renderRecords(await request('/api/workbench/records'));
        status.textContent = '待办已更新。';
      })));
    }
    target.append(row);
  }
  for (const operation of records.operations) {
    target.append(entry(`操作 ${operation.operation_id}`, `${operation.status} · ${operation.object_id} · ${operation.created_at}`).row);
  }
  for (const event of records.events) {
    const { row } = entry(event.kind, `${event.task_id} · ${event.created_at}`);
    const details = element('details');
    details.append(element('summary', '查看记录内容'), element('pre', JSON.stringify(JSON.parse(event.payload_json), null, 2)));
    row.append(details);
    target.append(row);
  }
  if (!target.childElementCount) target.append(element('p', '当前范围没有运行记录。', 'muted'));
}

function session() {
  const key = JSON.stringify(scope);
  if (!sessions.has(key)) sessions.set(key, { id: crypto.randomUUID(), log: [], draft: null });
  return sessions.get(key);
}

function renderChat() {
  chatForm.hidden = !chatAvailable || !scopeAvailable;
  byId('chat-state').textContent = chatAvailable ? '' : '对话执行器接入后可在这里发送消息。';
  byId('chat-log').replaceChildren(...session().log.map((turn) => element('li', turn)));
}

async function loadScope() {
  scopeAvailable = false;
  byId('invoice-count').textContent = '—';
  byId('bank-count').textContent = '—';
  for (const id of ['rule-list', 'memory-list', 'record-list']) byId(id).replaceChildren();
  memoryForm.elements.source_message_id.replaceChildren();
  memoryForm.elements.content.value = '';
  memoryForm.elements.user_wide.checked = false;
  byId('rule-search').elements.query.value = '';
  chatForm.elements.text.value = '';
  renderChat();
  const [records, rules, memories] = await Promise.all([
    request('/api/workbench/records'), request('/api/rules'), request('/api/memories'),
  ]);
  scopeAvailable = true;
  renderRecords(records);
  renderRules(rules);
  renderMemories(memories);
  renderChat();
  status.textContent = `${scope.user} · ${scope.enterprise} · ${scope.month}`;
}

scopeForm.addEventListener('submit', (event) => {
  event.preventDefault();
  if (busy) return;
  scope = Object.fromEntries(new FormData(scopeForm));
  run(loadScope);
});

document.querySelectorAll('[data-panel]').forEach((tab) => tab.addEventListener('click', () => {
  document.querySelectorAll('[data-panel]').forEach((item) => {
    byId(item.dataset.panel).hidden = item !== tab;
    if (item === tab) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
}));

byId('rule-search').addEventListener('submit', (event) => {
  event.preventDefault();
  const query = event.currentTarget.elements.query.value;
  run(async () => renderRules(await request('/api/rules', { query: { query } }), true));
});
byId('rule-search').addEventListener('reset', () => run(async () => renderRules(await request('/api/rules'))));
byId('refresh').addEventListener('click', () => run(async () => renderRecords(await request('/api/workbench/records'))));
byId('new-rule').addEventListener('click', () => {
  ruleForm.reset();
  byId('rule-error').textContent = '';
  ruleDialog.showModal();
});
byId('cancel-rule').addEventListener('click', () => ruleDialog.close());
ruleForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(ruleForm));
  run(async () => {
    await request('/api/rules', { method: 'POST', body });
    byId('rule-search').elements.query.value = '';
    renderRules(await request('/api/rules'));
    ruleDialog.close();
    status.textContent = '规则版本已保存。';
  }, byId('rule-error'));
});

memoryForm.elements.source_message_id.addEventListener('change', () => {
  memoryForm.querySelector('button').disabled = !scopeAvailable || !memoryForm.elements.source_message_id.value;
});
memoryForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const { content, source_message_id, user_wide } = memoryForm.elements;
  const body = { content: content.value, source_message_id: source_message_id.value, user_wide: user_wide.checked };
  run(async () => {
    await request('/api/memories', { method: 'POST', body });
    memoryForm.reset();
    renderMemories(await request('/api/memories'));
    status.textContent = '偏好已保存。';
  });
});

chatForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = chatForm.elements.text.value.trim();
  if (!text || !chatAvailable || !scopeAvailable) return;
  const current = session();
  if (current.draft?.text !== text) current.draft = { text, request_id: crypto.randomUUID() };
  run(async () => {
    const result = await request('/api/messages', { method: 'POST', body: {
      ...current.draft, session_id: current.id, enterprise_id: scope.enterprise, month: scope.month,
    } });
    const response = [`助手：${result.reply}`];
    for (const citation of result.citations || []) {
      response.push(`依据：${citation.name} ${citation.version}（${citation.doc_id} / ${citation.entry}）`);
    }
    if (result.pending_items?.length) response.push(`待办：${result.pending_items.join('、')}`);
    if (result.operation_ids?.length) response.push(`操作：${result.operation_ids.join('、')}`);
    current.log.push(`你：${text}`, response.join('\n'));
    current.draft = null;
    chatForm.reset();
    renderChat();
    renderRecords(await request('/api/workbench/records'));
  });
});

run(async () => {
  const schema = await fetch('/openapi.json').then((response) => response.json());
  chatAvailable = Boolean(schema.paths['/api/messages']?.post);
  await loadScope();
});
