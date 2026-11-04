'use strict';
(() => {
  const el = id => document.getElementById(id);
  const workbench = window.esgWorkbench;
  const ui = {reports:[], source:null, running:false, job:null, timer:null, run:null, tab:'extract', offset:0, total:0, revision:0};
  const names = {RUNNING:'运行中', PARTIAL:'已执行 · 待复核', FAILED:'未完成', CANCELLED:'已停止', INTERRUPTED:'已中断',
    QUEUED:'准备中', EXTRACT:'提取数据', FILTER:'A过滤', CLASSIFY:'A归类', AUDIT:'B独立审查', VALIDATE:'本地验证', IMPORT_REVIEW:'建立复核任务', FINISHED:'进入人工复核',
    CLASSIFIED:'已归类', UNKNOWN:'未知', INSUFFICIENT_EVIDENCE:'证据不足', esgagent_pdf:'新版PDF直读', ASSIGNED:'已归类', UNMAPPED:'未映射', AMBIGUOUS:'归类有歧义', INSUFFICIENT_CONTEXT:'缺少上下文', NEEDS_REVIEW:'待核对', RECOVERED_NEEDS_REVIEW:'格式恢复 · 待核对',
    native_table:'原生表格', vision_table:'视觉表格', vision_chart:'图表', vision_infographic:'信息图', vision_recovered_table:'恢复的视觉表格', vision_recovered_infographic:'恢复的信息图'};
  function element(tag, text, cls) { const n = document.createElement(tag); if(text !== undefined) n.textContent = String(text ?? '未知'); if(cls) n.className = cls; return n; }
  function error(message) { el('pipeline-error').hidden = !message; el('pipeline-error').textContent = message || ''; }
  function controls() {
    const busy=ui.running||ui.batchBusy;
    const model = el('pipeline-mode').value === 'model';
    el('pipeline-api-options').hidden = !model;
    el('pipeline-api-options').disabled = !model || busy;
    el('pipeline-source').disabled = busy;
    el('pipeline-upload').disabled = busy;
    el('pipeline-mode').disabled = busy;
    el('pipeline-start').disabled = busy || !ui.source || !ui.available;
    el('pipeline-view').disabled = ui.running || !ui.source?.workbench_run_id;
    el('pipeline-cancel').hidden = !ui.running || !!ui.batchBusy;
    el('pipeline-mode-help').textContent = model ? '新版EsgAgent直接理解完整PDF并归类，随后执行B审查和年度草稿。无匹配新版缓存时才调用提取模型。' : '仅复用新版EsgAgent缓存，不调用API；无匹配缓存会明确停止，不回退旧提取器。';
  }
  window.addEventListener('esg-batch-busy',e=>{ui.batchBusy=e.detail;controls();});
  function renderJob(job) {
    ui.job = job; ui.running = job.status === 'RUNNING';
    el('pipeline-progress').hidden = false;
    const text = `${job.name} · ${names[job.status] || job.status} · ${names[job.stage] || job.stage}：${job.message}`;
    if(el('pipeline-job-state').textContent !== text) el('pipeline-job-state').textContent = text;
    el('pipeline-cancel').disabled = job.cancel_requested;
    const u = job.usage;
    el('pipeline-job-usage').textContent = `本任务新增请求 ${u.calls} 次 · 输入 ${u.input_tokens.toLocaleString()} / 输出 ${u.output_tokens.toLocaleString()} token · 已知合计 ${u.total_tokens.toLocaleString()} · 未返回用量 ${u.unknown_requests} 次${job.reused_extraction ? ' · 已复用提取结果' : ''}`;
    const steps = Array.from(el('pipeline-steps').children);
    const stage=({COLLECTION_A:'CLASSIFY',COLLECTION_B:'AUDIT',COLLECTION_VALIDATE:'VALIDATE',COLLECTION_DONE:'FINISHED'})[job.stage]||job.stage;
    const current = steps.findIndex(s => s.dataset.stages.split(',').includes(stage));
    steps.forEach((s, i) => { s.classList.toggle('done', i < current); s.classList.toggle('current', i === current); if(i === current) s.setAttribute('aria-current','step'); else s.removeAttribute('aria-current'); });
    controls();
  }
  async function listJobs() {
    const {jobs} = await workbench.api('/api/pipeline/jobs');
    el('pipeline-jobs').replaceChildren();
    if(!jobs.length) el('pipeline-jobs').append(element('p','尚无流程任务。','muted'));
    jobs.forEach(job => { const button = element('button',`${job.name} · ${names[job.status] || job.status}`,'pipeline-job-button'); button.type='button'; button.onclick = () => selectJob(job.job_id).catch(e => error(e.message)); el('pipeline-jobs').append(button); });
    return jobs;
  }
  async function selectJob(id) {
    clearTimeout(ui.timer);
    const job = await workbench.api(`/api/pipeline/jobs/${id}`);
    renderJob(job);
    if(job.status === 'RUNNING') ui.timer = setTimeout(() => selectJob(id).catch(e => error(e.message)),1200);
    else {
      await listJobs();
      if(job.options.source_id) ui.source={...(ui.reports.find(r=>r.id===job.options.source_id) || {}),id:job.options.source_id,name:job.name,workbench_run_id:job.result_run_id};
      el('pipeline-consent').checked=false;
      controls();
      if(job.result_run_id) await workbench.selectRun(job.result_run_id);
      if((job.options.run_id||job.options.engine==='esgagent-v1') && job.result_run_id) await waterPreview(false);
      else if(job.status !== 'PARTIAL') {
        error(job.message);
        if(job.options.source_id) ui.source = {id:job.options.source_id, name:job.name};
        controls();
      }
    }
  }
  async function showResults(run, reset=true) {
    ui.run = run;
    if(reset) ui.offset = 0;
    const revision = ++ui.revision;
    const tab = ui.tab === 'review' ? 'classify' : ui.tab;
    const data = await workbench.api(`/api/pipeline/runs/${run}?tab=${tab}&offset=${ui.offset}&limit=25`);
    if(revision !== ui.revision) return;
    el('pipeline-data').hidden = !data.available;
    if(!data.available) return;
    el('water-legacy-tools').hidden=data.engine==='esgagent-v1';
    el('pipeline-data-title').textContent = data.name + ' · 全过程数据';
    el('pipeline-data-summary').textContent = `提取候选 ${data.candidate_count} 条 → A记录 ${data.record_count} 条 → B复核提示 ${data.b_findings} 项。当前${data.mode === 'model' ? '含受限模型运行' : '为本地规则模式'}，结果仍需核对。`;
    el('pipeline-export').href = `/api/pipeline/runs/${run}/export`;
    const extractionIssues = Object.entries(data.page_coverage).filter(([k]) => ['FAILED','MODEL_PARTIAL','PENDING_BUDGET','NOT_EXECUTED'].includes(k));
    const caution = extractionIssues.map(([k,n]) => `${({FAILED:'失败',MODEL_PARTIAL:'部分识别',PENDING_BUDGET:'待预算',NOT_EXECUTED:'未视觉识别'})[k]} ${n} 页`).join('，');
    el('pipeline-data-note').textContent = '候选数不等于独立指标数；没有发现数据不等于没有披露。' + (caution ? `提取覆盖提示：${caution}。` : '') + '详细问题及原文可在下方复核。';
    ui.total = data.total;
    el('pipeline-table-wrap').hidden = ui.tab === 'review';
    el('pipeline-pagination').hidden = ui.tab === 'review';
    el('pipeline-review-intro').hidden = ui.tab !== 'review';
    const heading = ui.tab === 'extract' ? ['PDF页','指标原文 / 上下文','原始值','单位','年份','来源','原页'] : ['PDF页','指标原文','原始值 / 单位','年份','A字段与状态','操作'];
    const tr = element('tr'); heading.forEach(t => {const th=element('th',t); th.scope='col'; tr.append(th);});
    el('pipeline-table-head').replaceChildren(tr); el('pipeline-table-body').replaceChildren();
    el('pipeline-table-caption').textContent = ui.tab === 'extract' ? '完整提取候选，按原始顺序分页' : 'A归类记录，保留原值与未确定状态';
    el('pipeline-table-wrap').setAttribute('aria-labelledby', `pipeline-tab-${ui.tab === 'review' ? 'classify' : ui.tab}`);
    data.rows.forEach(row => {
      const tr = element('tr');
      const values = ui.tab === 'extract' ? [row.page,row.label || '待绑定的表格单元格',row.value,row.unit,row.year,names[row.source] || row.source]
        : [row.page,row.label || '待识别',`${row.value ?? '未知'} ${row.unit || ''}`,row.year,`${data.engine==='esgagent-v1' ? row.metric || 'UNKNOWN' : workbench.profileName(row.field) || row.field || '未映射'} · ${names[row.upstream_status || row.status] || row.upstream_status || row.status}`];
      values.forEach(v => tr.append(element('td',v)));
      const cell = element('td'); const image = element('button','查看原页','text-button'); image.type='button'; image.onclick=()=>showPage(run,row.page); cell.append(image);
      if(ui.tab === 'classify') { const review=element('button','复核此条','text-button'); review.type='button'; review.onclick=()=>{workbench.selectRecord(row.id);}; cell.append(review); }
      tr.append(cell); el('pipeline-table-body').append(tr);
    });
    if(!data.rows.length) {const tr=element('tr'),td=element('td','本阶段没有生成记录；请核对PDF可读性、提取覆盖或闭集范围。');td.colSpan=heading.length;tr.append(td);el('pipeline-table-body').append(tr);}
    el('pipeline-page-count').textContent = data.total ? `${ui.offset+1}—${Math.min(ui.offset+25,data.total)} / ${data.total} 条` : '0 条';
    el('pipeline-prev').disabled = ui.offset === 0;
    el('pipeline-next').disabled = ui.offset + 25 >= data.total;
  }
  function showPage(run,page) {
    el('pipeline-image-title').textContent = `原页截图 · PDF第 ${page} 页`;
    el('pipeline-image').alt = `本报告第${page}个物理PDF页面`;
    el('pipeline-image').src = `/api/pipeline/runs/${run}/pages/${page}`;
    el('pipeline-image-pdf').href = `/api/runs/${run}/pdf#page=${page}`;
    if(!el('pipeline-image-dialog').open) el('pipeline-image-dialog').showModal();
  }
  document.addEventListener('esg:page', e=>showPage(e.detail.run,e.detail.page));
  document.addEventListener('esg:run-selected', e=>showResults(e.detail.run).catch(err=>error(err.message)));
  el('pipeline-image-close').onclick=()=>el('pipeline-image-dialog').close();
  el('pipeline-image').onerror=()=>{el('pipeline-image-title').textContent='截图暂不可用，请打开完整PDF核对';};
  function sourceFields(source){
    const match=/^(\d{5})(?:_|\b)/.exec(source?.name||'');
    el('pipeline-company').value=source?.company_code || match?.[1] || '';
    el('pipeline-year').value=source?.report_year || 2025;
  }
  el('pipeline-source').onchange=()=>{ui.source=ui.reports.find(r=>r.id===el('pipeline-source').value) || null;el('pipeline-upload').value='';sourceFields(ui.source);error('');controls();};
  el('pipeline-mode').onchange=()=>{el('pipeline-consent').checked=false;controls();};
  el('pipeline-upload').onchange=async()=>{
    const file=el('pipeline-upload').files[0];if(!file)return;
    error('');ui.source=null;ui.running=true;controls();
    try {
      if(file.size>64*1024*1024)throw new Error('PDF不能超过64 MB。');
      const response=await fetch('/api/pipeline/upload',{method:'POST',headers:{'Content-Type':'application/pdf','X-Workbench-Token':workbench.getToken(),'X-Filename':encodeURIComponent(file.name)},body:file});
      const data=await response.json();if(!response.ok)throw new Error(data.message || '上传未完成');
      ui.source=data;sourceFields(data);el('pipeline-source').value='';el('pipeline-upload-help').textContent=`已保存：${data.name} · ${data.pages}页。请点击运行完整流程。`;
    } catch(e){error(e.message);} finally{ui.running=false;controls();}
  };
  el('pipeline-view').onclick=()=>ui.source?.workbench_run_id && workbench.selectRun(ui.source.workbench_run_id).catch(e=>error(e.message));
  el('pipeline-form').onsubmit=async e=>{
    e.preventDefault();error('');if(!ui.source)return;
    const model=el('pipeline-mode').value==='model';
    if(model&&!el('pipeline-consent').checked){error('请先确认所选报告的API调用范围。');return;}
    const body={engine:'esgagent-v1',year:Number(el('pipeline-year').value),company_code:el('pipeline-company').value||null,
      source_id:ui.source.id,mode:model?'model':'rules_only',vision:false,allow_external:model&&el('pipeline-consent').checked,
      vision_calls:0,a_calls:model?Number(el('pipeline-a-calls').value):0,b_calls:model?Number(el('pipeline-b-calls').value):0};
    ui.running=true;controls();
    try{const response=await workbench.api('/api/pipeline/jobs',body);await selectJob(response.job_id);}catch(err){ui.running=false;controls();error(err.message);}
  };
  el('pipeline-cancel').onclick=async()=>{try{await workbench.api(`/api/pipeline/jobs/${ui.job.job_id}/cancel`,{});await selectJob(ui.job.job_id);}catch(e){error(e.message);}};
  for(const tab of ['extract','classify','review']) {
    el(`pipeline-tab-${tab}`).onclick=()=>{ui.tab=tab;['extract','classify','review'].forEach(t=>{el(`pipeline-tab-${t}`).setAttribute('aria-selected',String(t===tab));el(`pipeline-tab-${t}`).tabIndex=t===tab?0:-1;});if(ui.run)showResults(ui.run).catch(e=>error(e.message));};
    el(`pipeline-tab-${tab}`).onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const tabs=['extract','classify','review'];const index=e.key==='Home'?0:e.key==='End'?2:(tabs.indexOf(tab)+(e.key==='ArrowRight'?1:2))%3;el(`pipeline-tab-${tabs[index]}`).click();el(`pipeline-tab-${tabs[index]}`).focus();};
  }
  el('pipeline-prev').onclick=()=>{ui.offset=Math.max(0,ui.offset-25);showResults(ui.run,false).catch(e=>error(e.message));};
  el('pipeline-next').onclick=()=>{ui.offset+=25;showResults(ui.run,false).catch(e=>error(e.message));};
  el('pipeline-to-review').onclick=()=>document.querySelector('.review-grid').scrollIntoView({block:'start'});
  async function waterPreview(all) {
    const button=el(all?'water-preview-all':'water-preview');
    const run=workbench.currentRun();
    if(!all&&!run){el('water-status').textContent='请先选择一份已有流程结果。';return;}
    button.disabled=true;el('water-status').textContent='正在核对来源、财年、范围及B审查状态，无API调用…';
    try {
      const data=await workbench.api(all?'/api/water/collection':`/api/pipeline/runs/${run}/annual`);
      const body=el('water-preview-body');body.replaceChildren();
      const sources=new Map(data.reports.flatMap(report=>report.observations.map(o=>[o.observation_id,{...o,workbench_run_id:report.workbench_run_id}])));
      const labels=Object.fromEntries(data.contract.fields.map((f,i)=>[f,data.contract.headers[i]]));
      const names={ESGAGENT_DRAFT:'新版提取草稿 · 待B确认',INFERRED_DRAFT:'新版推定口径 · 待B确认',DERIVED_DRAFT:'新版公式推算 · 非直接披露',AUTO_VERIFIED:'通过自动准入',MODEL_REVIEWED:'LLM筛选＋B核对（研究值）',NO_REPORT:'尚无报告',NO_ELIGIBLE_YEAR_CANDIDATE:'该财年无合格候选',NO_ELIGIBLE_VALUE:'未通过准入',CONFLICT:'数值冲突'};
      const reasons={B_INSUFFICIENT:'B审查证据不足',INDEPENDENT_CELL_BINDING_UNPROVEN:'原页单元格对应未核实',COMPLETE_GROUP_BOUNDARY_UNPROVEN:'完整集团范围未核实',FISCAL_PERIOD_UNRESOLVED:'完整财年未核实',YEAR_UNRESOLVED:'数据年度不明',CONSUMPTION_DEFINITION_UNPROVEN:'真实耗水定义不明',NO_REPORT:'尚未取得报告',NO_ELIGIBLE_YEAR_CANDIDATE:'当前输入未取得该财年可采用值',NO_INDEPENDENT_VALUE:'尚无独立核实数值',A_CLASSIFICATION_UNRESOLVED:'A归类待定',HISTORICAL_PERIOD_BOUNDARY_UNPROVEN:'历史年度期间与范围待核实'};
      for(const row of data.rows.filter(r=>r.year===2025))for(const field of data.contract.fields.filter(f=>row.states[f])){
        const state=row.states[field];
        const flags=state.quality_flags||[];
        const quality=(state.approximate?' · 吨水按1t≈1m³近似换算':'')+(flags.some(f=>['ESG_REPORTED_REVENUE_REQUIRES_AUDITED_ANNUAL_CROSSCHECK','REVENUE_SOURCE_REQUIRES_ANNUAL_CROSSCHECK'].includes(f))?' · 收入待年报交叉核对':'')+(flags.includes('CURRENCY_INFERRED')?' · 币种按新版约定推定':'');
        const tr=element('tr');[row.stock_code,labels[field],state.value===null?'—':state.value,`${names[state.status]||state.status}${quality}${state.reasons.length?' · '+state.reasons.map(r=>reasons[r]||r).join(' / '):''}`].forEach(t=>tr.append(element('td',t)));
        const source=element('td'), proof=sources.get(state.source_ids[0]);
        if(proof&&proof.workbench_run_id&&proof.page){const button=element('button',`PDF第${proof.page}页`,'text-button');button.type='button';button.onclick=()=>showPage(proof.workbench_run_id,proof.page);source.append(button);}else source.textContent='—';
        tr.append(source);body.append(tr);
      }
      el('water-preview-wrap').hidden=false;
      const count=data.rows.filter(r=>r.year===2025).reduce((n,r)=>n+Object.values(r.states).filter(s=>s.value!==null).length,0);
      const issues=Object.entries(data.statistics.rejection_reasons).filter(([k])=>['B_INSUFFICIENT','INDEPENDENT_CELL_BINDING_UNPROVEN','FISCAL_PERIOD_UNRESOLVED'].includes(k)).map(([k,n])=>`${reasons[k]} ${n}条`).join('；');
      el('water-status').textContent=`已检查${data.statistics.reports}份报告、${data.statistics.companies}家公司；2025数值字段采用${count}项。原底册${data.registered_companies}家公司，尚未核实覆盖全体港股。未启用评分。${issues}`;
    } catch(e){el('water-status').textContent=e.message;} finally{button.disabled=false;}
  }
  el('water-preview').onclick=()=>waterPreview(false);
  el('water-preview-all').onclick=()=>waterPreview(true);
  el('water-run').onclick=async()=>{
    const run=workbench.currentRun();
    if(!run){el('water-status').textContent='请先选择一份已有结果的报告。';return;}
    if(!el('water-consent').checked){el('water-status').textContent='请先确认当前报告的API调用范围。';return;}
    const button=el('water-run');button.disabled=true;
    try {
      const result=await workbench.api('/api/water/jobs',{run_id:run,a_calls:Number(el('water-a-calls').value),b_calls:Number(el('water-b-calls').value),vision:el('water-vision').checked,allow_external:true});
      el('water-consent').checked=false;el('water-status').textContent='年度筛选已启动，可在上方任务进度查看用量或停止后续调用。';
      await selectJob(result.job_id);
    }catch(e){el('water-status').textContent=e.message;}finally{button.disabled=false;}
  };
  workbench.ready.then(async data=>{
    if(!data)return;
    ui.available=data.pipeline?.available;el('pipeline-capability').textContent=data.pipeline?.message || '运行环境未知。';
    const option=el('pipeline-mode').querySelector('option[value="model"]');option.disabled=!data.pipeline?.api_key_configured;
    if(option.disabled)option.textContent='模型模式 · 本机未配置密钥';
    const reports=await workbench.api('/api/pipeline/reports');ui.reports=reports.reports;
    ui.reports.forEach(r=>{const option=element('option',`${r.name} · ${r.pages}页`);option.value=r.id;el('pipeline-source').append(option);});
    const jobs=await listJobs();const active=jobs.find(j=>j.status==='RUNNING');if(active)await selectJob(active.job_id);
    if(workbench.currentRun())await showResults(workbench.currentRun());
    controls();
  }).catch(e=>error(e.message));
})();
