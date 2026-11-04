'use strict';
const $ = id => document.getElementById(id);
const state = {token: '', detail: null, selected: null, configured: false, bundleId: null, busy: false, polling: null, bConfigured:false, bFiles:null};
const labels = {OPEN:'待处理', RESOLVED:'已人工确认', REAUDIT_REQUIRED:'修订待重审', EXCLUDED_FROM_SCORING:'已申请排除评分',
  RUNNING:'审查中', SUCCEEDED:'审查完成', PARTIAL:'部分完成 / 需复核', FAILED:'运行失败', INTERRUPTED:'运行已中断',
  CONFIRMED:'审查确认', SUSPECT:'存在疑点', REJECTED:'审查不符', INSUFFICIENT:'信息不足', NOT_REVIEWED:'未审查', NOT_APPLICABLE:'不适用数值审查', BLOCK:'阻断', WARN:'提醒', INFO:'信息',
  b_demo:'B合成演示', b_import:'B结果导入 · 未核验A契约', b_formal:'B正式接口运行',
  rules_only:'B规则检查', model:'B模型运行',
  KEEP:'保留', REMAP:'修订变量', CORRECT_VALUE:'修订数值', DEFER:'暂缓', offline:'离线', http:'真实模型', demo_replay:'合成演示回放'};
function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = String(text); if (cls) n.className = cls; return n; }
function pill(value) { return node('span', labels[value] || value, `pill ${value}`); }
const profileLabels = {water_withdrawal_m3:'取水量',water_consumption_m3:'耗水量',water_discharge_m3:'排水量',water_recycled_reused_m3:'回用水量',water_recycling_rate_pct:'水回用率',cod_discharge_t:'COD排放量',ammonia_n_discharge_t:'氨氮排放量',total_n_discharge_t:'总氮排放量',total_p_discharge_t:'总磷排放量',revenue_million:'营业收入',revenue_currency:'收入币种'};
function recordTitle(record) { return record?.display_indicator_id || profileLabels[record?.profile_field] || (record?.variable === 'UNMAPPED' ? (record.classification_reason || '未映射，需核对上游归类').slice(0,65) : record?.variable); }
function notice(text, error = false) { $('notice').textContent = text; $('notice').className = error ? 'notice error' : 'notice'; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Workbench-Token':state.token}, body:JSON.stringify(body)});
  const data = await response.json(); if (!response.ok) throw new Error(data.message || '操作未完成'); return data;
}
function busy(value) {
  state.busy = value; $('demo').disabled = value; $('upload').disabled = value;
  $('audit').disabled = value || !state.bundleId || state.detail?.engine === 'b';
  $('b-demo').disabled = value; $('b-files').disabled = value;
  $('b-import').disabled = value || !state.bFiles || !bOutputs.every(n => n in state.bFiles);
  $('b-run').disabled = value || !state.bConfigured || !state.bFiles || !bInputs.every(n => n in state.bFiles);
  $('b-pdf').disabled = value || !state.detail || state.detail.engine !== 'b';
}
async function bootstrap() {
  const data = await api('/api/bootstrap'); state.token = data.token; state.configured = data.http_model_configured;
  state.bConfigured = Boolean(data.b_audit?.configured); $('b-config').textContent = data.b_audit?.message || 'B接入状态未知。';
  const option = $('mode').querySelector('option[value="http"]'); option.disabled = !state.configured;
  option.textContent = state.configured ? `真实模型 · ${data.model}` : '真实模型 · 尚未配置';
  $('runs').replaceChildren();
  $('run-picker').replaceChildren(node('option','选择历史审查'));
  $('run-picker').firstChild.value = '';
  if (!data.runs.length) $('runs').append(node('p','尚无审查记录','empty'));
  data.runs.forEach(run => {
    const option = node('option',`${run.report_context.company_name} · ${labels[run.execution_mode] || labels[run.provider] || run.provider} · ${labels[run.status] || run.status}`);
    option.value = run.run_id; option.selected = state.detail?.run_id === run.run_id; $('run-picker').append(option);
    const button = node('button', null, `run-item ${state.detail?.run_id === run.run_id ? 'active' : ''}`); button.type = 'button';
    button.append(node('strong', run.report_context.company_name), node('small', `${run.report_context.report_year} · ${labels[run.execution_mode] || labels[run.provider] || run.provider}${run.execution_mode === 'model' ? ' · 模型已审 ' + (run.model_records_reviewed ?? '—') + ' 条' : ''} · ${labels[run.status] || run.status}`));
    button.onclick = () => selectRun(run.run_id).catch(showError); $('runs').append(button);
  }); busy(state.busy); return data;
}
function showError(error) { console.error(error); notice(error.message || '操作失败，请重试', true); busy(false); }
async function selectRun(id) {
  clearTimeout(state.polling); const detail = await api(`/api/runs/${id}`);
  state.detail = detail; state.bundleId = detail.bundle_id;
  $('mode').disabled = detail.engine === 'b';
  $('mode').hidden = detail.engine === 'b'; $('audit').hidden = detail.engine === 'b';
  document.querySelector('label[for="mode"]').hidden = detail.engine === 'b';
  if (!detail.tasks.some(t => t.task_id === state.selected)) state.selected = detail.tasks[0]?.task_id || null;
  history.replaceState(null, '', '#' + id); render();
  document.dispatchEvent(new CustomEvent('esg:run-selected',{detail:{run:id}}));
  if (detail.status === 'RUNNING') state.polling = setTimeout(() => selectRun(id).catch(showError), 900);
  else { busy(false); await bootstrap(); }
}
function render() {
  const d = state.detail, b = d.bundle;
  $('company').textContent = b.report_context.company_name;
  $('report-tag').textContent = `${d.is_demo ? 'SYNTHETIC DEMO' : 'REPORT AUDIT'} / ${b.report_context.pid}`;
  $('report-meta').textContent = `${b.report_context.report_year} 年度 · ${b.catalogue.version} · ${labels[d.provider] || d.provider}`;
  $('run-state').textContent = `${labels[d.status] || d.status}${d.status === 'RUNNING' ? ` · 已处理 ${d.progress}/${d.total || '…'} 批` : ''}`;
  const open = d.tasks.filter(t => t.state !== 'RESOLVED' && t.state !== 'EXCLUDED_FROM_SCORING');
  $('block-count').textContent = open.filter(t => t.priority === 'P0').length;
  $('open-count').textContent = open.length; $('record-count').textContent = b.records.length; $('evidence-count').textContent = b.evidence.length;
  $('integrity').textContent = d.result ? `INPUT SHA256 · ${d.result.manifest.input_sha256.slice(0,16)}…` : '';
  $('export').classList.toggle('disabled', d.status === 'RUNNING'); $('export').setAttribute('aria-disabled', String(d.status === 'RUNNING'));
  $('export').href = `/api/runs/${d.run_id}/export`; busy(d.status === 'RUNNING');
  if (d.is_demo) notice('合成演示：使用预设模型响应验证流程，未调用真实 AI，不代表任何真实公司的披露或模型准确率。');
  else if (d.provider === 'b_import') notice('B结果已导入：仅进行本地文件一致性检查，未核验A正式契约，也未重新调用模型。人工意见单独留存。');
  else if (d.provider === 'b_formal') notice('B接口运行：由本机配置绑定A契约与客户端。人工修订仅生成请求，仍需新版本重审及S5验证。');
  else if (d.provider === 'offline') notice('离线审查：没有执行 AI 语义判断，所有未确认项转人工复核；不会自动判为通过。');
  else notice('真实模型审查：请对照证据核对建议。人工修订会生成重审请求，不改写原始记录。');
  if (d.result?.b_report?.validation_scope === 'local_delivery') {
    notice(d.result.b_report.execution_mode === 'rules_only'
      ? 'A真实数据 · B规则检查：未调用AI。未完成核验保留在台账，只有具体问题进入人工队列；当前为本地兼容适配，尚未验收A正式契约。'
      : 'A真实数据 · B模型审查：已接入本地适配流程，请核对模型调用、失败和未审查统计；尚未验收A正式契约，待确认换算不自动通过。');
  }
  if (d.error) notice(`运行未完成：${d.error}。输入已保留，可再次启动审查。`, true);
  $('b-summary').hidden = d.engine !== 'b';
  if (d.engine === 'b') {
    const report = d.result?.b_report, stats = report?.stats || {};
    $('b-summary-text').textContent = report ? `原始批次 ${report.audit_run_id} · ${labels[report.status] || report.status} · 模型调用 ${stats.model_calls ?? '—'} · 人工具体问题 ${d.tasks.length} 项 · 待自动核验 ${d.review_routing?.unverified_records?.length||0} 条 · 定性披露 ${d.numeric_review_applicability?.not_applicable_records||0} 条不适用数值审查。非待办历史 ${d.archived_nonactionable_tasks?.length||0} 项保留在导出包。没有人工任务不表示已审核通过。` : '正在运行或未能生成完整结果；失败原因将显示在上方，输入可导出保留。';
    $('b-problems').replaceChildren();
    $('b-problems-title').textContent = `技术状态与自动核验提示（${report?.problems?.length || 0}项，不自动转人工）`;
    (report?.problems || []).forEach(p => $('b-problems').append(node('li',`${p.error_code || '运行提示'}${p.record_id ? ' / ' + p.record_id : ''}：${p.message || ''}`)));
    $('b-unverified-records').replaceChildren();
    (d.review_routing?.unverified_records||[]).forEach(id=>{
      const record=d.bundle.records.find(r=>r.record_uid===id);if(!record)return;
      const row=node('tr'),verdict=d.result.record_verdicts.find(v=>v.record_uid===id);
      [recordTitle(record),record.year||'年度未明确',`${d.display_values[id]??'无可用数值'} ${record.unit||''}`,labels[verdict?.verdict]||verdict?.verdict||'待核验'].forEach(value=>row.append(node('td',value)));
      $('b-unverified-records').append(row);
    });
  }
  renderQueue(); renderSelection();
}
function renderQueue() {
  const d = state.detail, filter = $('filter').value; $('queue').replaceChildren(); $('queue-count').textContent = d.tasks.length;
  const tasks = d.tasks.filter(t => filter === 'ALL' || t.state === filter || (filter === 'P0' && t.priority === 'P0' && t.state === 'OPEN'));
  tasks.forEach(task => {
    const record = d.bundle.records.find(r => r.record_uid === task.record_uid);
    const button = node('button', null, 'task' + (task.task_id === state.selected ? ' active' : '')); button.type = 'button';
    button.setAttribute('aria-pressed', String(task.task_id === state.selected));
    button.append(pill(task.state), node('span',recordTitle(record) || task.title || '指标组遗漏检查','task-title'), node('span',task.review_question || task.title,'form-help'), node('span',`${task.indicator_id} · ${record?.year || '—'} · ${task.priority}`,'task-meta'));
    if(task.review_status) button.append(pill(task.review_status));
    button.onclick = () => { state.selected = task.task_id; renderQueue(); renderSelection(); }; $('queue').append(button);
  });
  if (!tasks.length) $('queue').append(node('p', d.status === 'RUNNING' ? '审查进行中，完成后生成队列。' : '当前筛选下没有复核任务。', 'empty'));
}
function sourceDOM(content) {
  // Tokenize only table/text tags. No HTML parser, URLs, attributes, scripts or handlers are ever inserted.
  const root = node('div', null, 'source-content'), stack = [root];
  const allowed = new Set(['table','thead','tbody','tr','th','td','p','br','strong','em']);
  const tokens = content.split(/(<[^>]*>)/g);
  tokens.forEach(token => {
    const match = token.match(/^<\s*(\/?)\s*([a-zA-Z0-9]+)[^>]*>$/);
    if (match) {
      const tag = match[2].toLowerCase();
      if (!allowed.has(tag)) { stack[stack.length-1].append(document.createTextNode(token)); return; }
      if (match[1]) { if (stack.length > 1 && stack[stack.length-1].tagName.toLowerCase() === tag) stack.pop(); }
      else { const element = node(tag); stack[stack.length-1].append(element); if (tag !== 'br') stack.push(element); }
    } else {
      // Decode only common entities, without assigning untrusted HTML.
      const text = token.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&nbsp;/g,' ').replace(/&amp;/g,'&');
      stack[stack.length-1].append(document.createTextNode(text));
    }
  }); return root;
}
function renderSelection() {
  const d = state.detail, task = d.tasks.find(t => t.task_id === state.selected), result = d.result;
  ['record','evidence','findings','history'].forEach(id => $(id).replaceChildren()); $('review-form').hidden = !task;
  if (!task) {
    $('evidence').append(node('p', d.status === 'RUNNING' ? '正在审查，请稍候…' : '没有待复核任务。可查看原始证据或导出完整结果。','empty'));
    d.bundle.evidence.slice(0,3).forEach(e => appendEvidence(e, d));
    $('findings').append(node('p', `没有需要人工处置的具体问题；仍有${d.review_routing?.unverified_records?.length||0}条待自动核验，不代表已通过S5。`, 'empty')); return;
  }
  const record = d.bundle.records.find(r => r.record_uid === task.record_uid);
  const findings = (result?.findings || []).filter(f => task.finding_ids.includes(f.finding_id));
  const verdict = result?.record_verdicts.find(v => v.record_uid === task.record_uid);
  if (record) {
    const qualitative=record.numeric_audit_applicability==='NOT_APPLICABLE';
    const card = node('div', null, 'record-card'); card.append(node('p', `${record.display_indicator_id || record.indicator_id} / 原始归类值`, 'eyebrow'), node('h4', recordTitle(record), 'record-title'));
    const rawValue = d.display_values[record.record_uid];
    const parts = (rawValue || '').split('.');
    const displayValue = /e/i.test(rawValue || '') ? rawValue : parts[0].replace(/\B(?=(\d{3})+(?!\d))/g,',') + (parts.length > 1 ? '.' + parts[1] : '');
    const value = node('div', qualitative ? '定性披露' : rawValue === null ? (record.source_available === false ? '未附加原始记录' : '无可用数值') : displayValue, 'record-value'); value.append(node('small', qualitative ? '不适用数值单位' : record.unit || '单位缺失'));
    const dimensions = node('div', null, 'record-dimensions'); dimensions.append(pill(String(record.year || '年度缺失')), pill(record.breakdown || record.scope_type || '范围待核对'), pill(qualitative?'NOT_APPLICABLE':verdict?.verdict || 'INSUFFICIENT'));
    card.append(value, dimensions);
    if(record.profile_id) {
      card.append(node('p',`上游归类：${record.classification_status} · 范围 ${record.scope_type || '未知'} · ${record.scope_note || ''}`,'form-help'));
      if(record.classification_reason) card.append(node('p',record.classification_reason,'form-help'));
      if(record.normalized_value !== null && record.normalized_value !== undefined) card.append(node('p',`上游归一值：${record.normalized_value} ${record.normalized_unit || ''} · 换算仍以审查状态为准`,'form-help'));
    }
    $('record').append(card);
  }
  const ids = new Set([record?.evidence_id, ...findings.flatMap(f => f.evidence_ids)]);
  let evidence = d.bundle.evidence.filter(e => ids.has(e.evidence_id));
  if (!record && !evidence.length) evidence = d.bundle.evidence.slice(0,5);
  if (!evidence.length) $('evidence').append(node('p','引用的原始证据不存在。请补充上游证据，不能仅凭数值确认。','empty'));
  evidence.forEach(e => appendEvidence(e, d));
  if (verdict) { const qualitative=record?.numeric_audit_applicability==='NOT_APPLICABLE';const box = node('div', null, 'finding'); box.append(pill(qualitative?'NOT_APPLICABLE':verdict.verdict), node('p',task.review_reason || (qualitative?'定性披露不适用数值审查。':verdict.reason)));if(task.review_question)box.append(node('p','需要确认：'+task.review_question)); $('findings').append(box); }
  findings.forEach(f => {
    const box = node('article', null, 'finding'); box.append(pill(f.severity), node('span',f.code,'code'), node('h4',f.reason));
    if (f.observed) box.append(node('p',`当前记录：${f.observed}`)); if (f.expected) box.append(node('p',`需核对：${f.expected}`));
    if (f.evidence_span) box.append(node('div', f.evidence_span, 'quote'));
    const confidence = Number.isFinite(f.confidence) ? `${(f.confidence * 100).toFixed(0)}%` : '未提供';
    box.append(node('p', d.engine === 'b' ? `来源：${f.model_call_id ? '模型审查' : '无模型调用标识 / 规则结果'} · 置信值 ${confidence} · 非准确率` : `模型自报置信度 ${confidence} · 非准确率`,'form-help')); $('findings').append(box);
  });
  if (!verdict && !findings.length) $('findings').append(node('p','本指标组审查未完成，请核对证据覆盖和遗漏情况。','empty'));
  $('action').value = 'DEFER'; $('reason').value = ''; $('new-value').value = ''; $('new-variable').replaceChildren();
  const indicator = d.bundle.catalogue.indicators.find(i => i.indicator_id === task.indicator_id);
  (indicator?.variables || []).forEach(v => { const option = node('option',v); option.value = v; $('new-variable').append(option); });
  const terminal = ['REAUDIT_REQUIRED','EXCLUDED_FROM_SCORING'].includes(task.state);
  Array.from($('review-form').elements).forEach(e => e.disabled = terminal);
  Array.from($('action').options).forEach(o => o.disabled = ((!record || record.source_available === false) && ['REMAP','CORRECT_VALUE','EXCLUDE_FROM_SCORING'].includes(o.value)) || (o.value === 'REMAP' && !indicator?.variables?.length));
  $('form-state').textContent = terminal ? '已形成下游处理请求。请生成新记录版本并重新审查，本页保留原始依据。' : `当前状态：${labels[task.state]} · 版本 ${task.version}`;
  actionFields();
  const events = d.events.filter(e => e.task_id === task.task_id).slice().reverse();
  if (events.length) { const history = node('div', null, 'history'); history.append(node('h4','复核历史'));
    events.forEach(e => { const row = node('div', null, 'event'); row.append(node('strong',`${e.reviewer} · ${labels[e.action] || e.action}`), node('p',e.reason), node('p', new Date(e.created_at).toLocaleString())); if (e.proposed_value !== undefined && e.proposed_value !== null) row.append(node('p',`候选值：${e.proposed_value}`)); if(e.proposed_variable) row.append(node('p',e.proposed_variable)); history.append(row); }); $('history').append(history); }
}
function appendEvidence(e, d) {
  const item = node('article', null, 'evidence-item'), meta = node('div',null,'evidence-meta');
  meta.append(node('strong', e.page_no ? `第 ${e.page_no} 页` : '页码缺失'), node('span',`${e.kind} · ${d.is_demo ? '合成原文' : '提取证据'}`));
  item.append(meta, sourceDOM(e.content), node('p',`${e.evidence_id}${e.bbox ? ' · bbox [' + e.bbox.join(', ') + '] PDF point' : ' · 无区域坐标'}`,'source-footnote'));
  if(d.has_pdf) {
    if(d.pipeline_available && e.page_no) {const button=node('button','在工作台查看原页','button secondary');button.type='button';button.onclick=()=>document.dispatchEvent(new CustomEvent('esg:page',{detail:{run:d.run_id,page:e.page_no}}));item.append(button);}
    const link = node('a','在新窗口查看附加 PDF 原页 ↗','pdf-link'); link.href=`/api/runs/${d.run_id}/pdf${e.page_no ? '#page=' + e.page_no : ''}`; link.target='_blank'; link.rel='noopener'; item.append(link); item.append(node('p',`附件 SHA256 ${d.pdf_sha256.slice(0,16)}… · 请核对附件与本报告一致。`,'source-footnote')); }
  else item.append(node('p','未附加原始 PDF，当前显示上游证据文本。','source-footnote'));
  $('evidence').append(item);
}
function actionFields() { $('value-field').hidden = $('action').value !== 'CORRECT_VALUE'; $('variable-field').hidden = $('action').value !== 'REMAP'; $('new-value').required = !$('value-field').hidden; }
const bInputs = ['records.jsonl','evidence.jsonl','semantic_filter.jsonl','document_context.json'];
const bOutputs = ['audit_findings.jsonl','audit_results.jsonl','audit_report.json','review_items.jsonl'];
const bAllowed = new Set([...bInputs,...bOutputs,'filter_audit_sample.jsonl','coverage.json','catalogue.json']);
$('b-files').onchange = async () => {
  state.bFiles = null; busy(true);
  try {
    const selected = Array.from($('b-files').files), files = {};
    if(selected.reduce((sum,f) => sum + f.size, 0) > 7*1024*1024) throw new Error('所选文本合计不能超过7 MB。');
    for(const file of selected) {
      if(!bAllowed.has(file.name)) throw new Error(`不支持的文件：${file.name}。请选择B约定的JSON/JSONL文件。`);
      if(file.name in files) throw new Error(`同名文件重复：${file.name}。请选择同一报告和批次的文件。`);
      files[file.name] = await file.text();
    }
    state.bFiles = selected.length ? files : null;
    const missingOut = bOutputs.filter(n => !(n in files)), missingIn = bInputs.filter(n => !(n in files));
    $('b-file-state').textContent = selected.length ? `已选 ${selected.length} 个文件。${missingOut.length ? '结果导入还缺：' + missingOut.join('、') : '四类结果已齐，可导入。'}${missingIn.length ? ' 页面运行还缺：' + missingIn.join('、') : ' 运行输入已齐。'}` : '尚未选择文件。';
  } catch(e) { $('b-file-state').textContent = e.message; showError(e); }
  finally { busy(false); }
};
async function bAction(path, body) {
  busy(true); notice('正在处理B审查数据，请稍候…');
  try { const response = await api(path,body); await selectRun(response.run_id); } catch(e) { showError(e); }
}
$('b-demo').onclick = () => bAction('/api/b/demo',{});
$('b-import').onclick = () => state.bFiles && bAction('/api/b/import',{files:state.bFiles});
$('b-run').onclick = () => {
  if(!state.bFiles || !state.bConfigured) return;
  if(!confirm('将使用本机配置的A客户端执行B审查，证据可能发送到模型服务并产生费用。确认运行？')) return;
  bAction('/api/b/run',{files:Object.fromEntries(Object.entries(state.bFiles).filter(([n]) => !bOutputs.includes(n)))});
};
$('b-pdf').onchange = async () => {
  const file = $('b-pdf').files[0], run = state.detail?.run_id; if(!file || !run) return;
  busy(true);
  try {
    if(file.size > 64*1024*1024) throw new Error('PDF附件不能超过64 MB。');
    const response = await fetch(`/api/runs/${run}/pdf`, {method:'POST',headers:{'Content-Type':'application/pdf','X-Workbench-Token':state.token},body:file});
    const data = await response.json(); if(!response.ok) throw new Error(data.message || '附件未保存。');
    await selectRun(run); notice('PDF已附加，可从证据卡片打开原页；本次没有重新执行视觉审查。');
  } catch(e) { showError(e); } finally { $('b-pdf').value=''; busy(false); }
};
$('action').onchange = actionFields; $('filter').onchange = () => state.detail && renderQueue();
$('refresh').onclick = () => bootstrap().catch(showError);
$('run-picker').onchange = () => $('run-picker').value && selectRun($('run-picker').value).catch(showError);
$('demo').onclick = async () => { busy(true); notice('正在载入合成演示并运行预设审查…'); try { const data = await api('/api/demo',{}); await selectRun(data.run_id); } catch(e) { showError(e); } };
$('upload').onchange = async () => {
  const file = $('upload').files[0]; if (!file) return; busy(true);
  try { if(file.size > 8*1024*1024) throw new Error('输入 JSON 不能超过 8 MB');
    const data = JSON.parse((await file.text()).replace(/^\uFEFF/,'')); const imported = await api('/api/import',data); state.bundleId = imported.bundle_id;
    notice('导入成功，正在执行离线审查并建立人工复核任务…'); const run = await api('/api/runs',{bundle_id:state.bundleId,mode:'offline'}); await selectRun(run.run_id);
  } catch(e) { showError(e); } finally { $('upload').value = ''; }
};
$('audit').onclick = async () => {
  if (!state.bundleId) return;
  if($('mode').value === 'http' && !confirm('启动真实模型审查会将本报告的证据片段和归类记录发送至你配置的模型服务，并可能产生费用。是否继续？')) return;
  busy(true); try { const data = await api('/api/runs',{bundle_id:state.bundleId, mode:$('mode').value}); await selectRun(data.run_id); } catch(e) { showError(e); }
};
$('review-form').onsubmit = async event => {
  event.preventDefault(); const task = state.detail.tasks.find(t => t.task_id === state.selected); if(!task) return;
  const decision = {action:$('action').value,reviewer:$('reviewer').value.trim(),reason:$('reason').value.trim(),expected_version:task.version};
  if (!decision.reviewer || !decision.reason) { notice('请填写复核人和处理依据。',true); return; }
  if(decision.action === 'CORRECT_VALUE') { if($('new-value').value === '') return; decision.proposed_value = Number($('new-value').value);
    if(!Number.isFinite(decision.proposed_value) || (Number.isInteger(decision.proposed_value) && !Number.isSafeInteger(decision.proposed_value))) { notice('候选值超过浏览器可安全提交的范围，请通过上游生成新记录版本。',true); return; } }
  if(decision.action === 'REMAP') decision.proposed_variable = $('new-variable').value;
  if(decision.action !== 'DEFER' && !confirm('确认保存此复核意见？原始记录保持不变，处理依据将留入历史。')) return;
  $('save').disabled = true;
  try { await api(`/api/runs/${state.detail.run_id}/tasks/${task.task_id}/review`,decision); localStorage.setItem('esg-reviewer',decision.reviewer); await selectRun(state.detail.run_id); notice('复核意见已保存。修订请求仍需上游生成新版本并重新审查；人工确认仍需 S5 验证。'); }
  catch(e) { showError(e); $('save').disabled = false; }
};
window.esgWorkbench = {api, selectRun, bootstrap, getToken:()=>state.token, currentRun:()=>state.detail?.run_id,
  profileName:field=>profileLabels[field],
  selectRecord:id=>{const task=state.detail?.tasks.find(t=>t.record_uid===id);if(task){state.selected=task.task_id;renderQueue();renderSelection();document.querySelector('.review-grid').scrollIntoView({block:'start'});}}};
window.esgWorkbench.ready = (async () => { try { $('reviewer').value = localStorage.getItem('esg-reviewer') || ''; const data = await bootstrap(); const id = location.hash.slice(1); if (data.runs.some(r => r.run_id === id)) await selectRun(id); else if (data.runs.length) await selectRun(data.runs[0].run_id); return data; } catch(e) { showError(e); } })();
