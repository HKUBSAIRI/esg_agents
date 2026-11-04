'use strict';
(() => {
  const el=id=>document.getElementById(id), wb=window.esgWorkbench;
  const state={draft:[],uploading:false,busy:false,available:false,selected:null,timer:null,reports:[]};
  const names={QUEUED:'排队中',RUNNING:'运行中',PARTIAL:'已执行 · 待复核',FAILED:'失败',CANCELLED:'已停止',INTERRUPTED:'已中断',SKIPPED:'重复跳过',COMPLETED:'队列完成',COMPLETED_WITH_ERRORS:'队列完成 · 部分失败',EXTRACT:'提取',CLASSIFY:'A归类',AUDIT:'B审查',VALIDATE:'质量校验',IMPORT_REVIEW:'年度归集',FINISHED:'待复核'};
  const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text??'');if(cls)n.className=cls;return n;};
  function error(message){el('batch-error').hidden=!message;el('batch-error').textContent=message||'';}
  function controls(){
    const busy=state.busy||state.uploading, model=el('batch-mode').value==='model';
    for(const id of ['batch-upload','batch-year','batch-existing','batch-add-existing','batch-mode','batch-a-calls','batch-b-calls','batch-clear','batch-extract-input','batch-extract-output','batch-audit-input','batch-audit-output','batch-price-note','batch-consent'])el(id).disabled=busy;
    document.querySelectorAll('#batch-draft-body input,#batch-draft-body button').forEach(n=>n.disabled=busy);
    el('batch-model-options').hidden=!model;
    el('batch-start').disabled=busy||!state.available||!state.draft.some(x=>x.id);
    el('batch-cancel').hidden=!state.busy;
    const count=state.draft.filter(x=>x.id).length;
    el('batch-budget-summary').textContent=model?`${count}份报告；每份提取最多${el('batch-a-calls').value}次、B最多${el('batch-b-calls').value}次，本批合计最多${count*(Number(el('batch-a-calls').value)+Number(el('batch-b-calls').value))}次请求。缓存命中不消耗提取次数。`:'缓存模式不调用API；没有缓存的报告会记录失败并继续下一份。';
    window.dispatchEvent(new CustomEvent('esg-batch-busy',{detail:busy}));
  }
  function draft(){
    const body=el('batch-draft-body');body.replaceChildren();
    state.draft.forEach((row,index)=>{
      const tr=node('tr');tr.append(node('td',`${row.name}${row.pages?' · '+row.pages+'页':''}`));
      for(const [field,type] of [['company_code','text'],['year','number']]){
        const td=node('td'),input=node('input');input.type=type;input.value=row[field]??'';
        input.setAttribute('aria-label',`${row.name}的${field==='year'?'财年':'公司代码'}`);
        if(field==='company_code'){input.maxLength=5;input.pattern='[0-9]{5}';input.inputMode='numeric';}
        else{input.min='2000';input.max='2100';input.required=!!row.id;}
        input.oninput=()=>{row[field]=field==='year'?Number(input.value):input.value;el('batch-consent').checked=false;};
        td.append(input);tr.append(td);
      }
      tr.append(node('td',row.error||'已添加到本机，待提交'));
      const td=node('td'),button=node('button','移除','button secondary');button.type='button';button.onclick=()=>{state.draft.splice(index,1);el('batch-consent').checked=false;draft();};td.append(button);tr.append(td);body.append(tr);
    });
    el('batch-draft-wrap').hidden=!state.draft.length;controls();
  }
  function add(source){
    const match=source.name.match(/^(\d{5})(?:_|\b)/);
    state.draft.push({...source,company_code:source.company_code||match?.[1]||'',year:source.report_year||Number(el('batch-year').value)});
  }
  el('batch-upload').onchange=async()=>{
    const files=Array.from(el('batch-upload').files||[]);if(!files.length)return;
    if(state.draft.length+files.length>100){error('每批最多100份，请减少选择。');el('batch-upload').value='';return;}
    error('');state.uploading=true;el('batch-consent').checked=false;controls();
    for(let i=0;i<files.length;i++){
      const file=files[i];el('batch-upload-status').textContent=`正在保存 ${i+1}/${files.length}：${file.name}`;
      try{
        if(file.size>64*1024*1024)throw new Error('超过64 MB，未上传');
        const response=await fetch('/api/pipeline/upload',{method:'POST',headers:{'Content-Type':'application/pdf','X-Workbench-Token':wb.getToken(),'X-Filename':encodeURIComponent(file.name)},body:file});
        const value=await response.json();if(!response.ok)throw new Error(value.message||'上传失败');add({...value,name:file.name.slice(0,160)});
      }catch(e){state.draft.push({name:file.name,year:Number(el('batch-year').value),company_code:'',error:e.message});}
      draft();
    }
    state.uploading=false;el('batch-upload').value='';
    el('batch-upload-status').textContent=`本机已添加${state.draft.filter(x=>x.id).length}份；上传失败${state.draft.filter(x=>!x.id).length}份（不会提交，错误保留在下表）。请核对公司、财年和调用范围。`;draft();
  };
  el('batch-add-existing').onclick=()=>{
    const selected=Array.from(el('batch-existing').selectedOptions).map(o=>state.reports.find(r=>r.id===o.value));
    if(state.draft.length+selected.length>100){error('每批最多100份。');return;}
    selected.forEach(add);el('batch-consent').checked=false;draft();
  };
  el('batch-clear').onclick=()=>{state.draft=[];el('batch-consent').checked=false;el('batch-upload-status').textContent='待提交列表已清空；已保存PDF及历史队列保留。';draft();};
  for(const id of ['batch-mode','batch-a-calls','batch-b-calls','batch-year'])el(id).oninput=()=>{el('batch-consent').checked=false;controls();};
  function usage(u){return `${u?.calls||0}次请求 · 输入${(u?.input_tokens||0).toLocaleString()} / 输出${(u?.output_tokens||0).toLocaleString()} token${u?.unknown_requests?' · '+u.unknown_requests+'次用量未知':''}`;}
  function cost(c,u){if(!u?.calls)return '无新增请求';if(c?.unknown_cost_requests)return `已知估算¥${c.known_estimate_cny.toFixed(4)}；${c.unknown_cost_requests}次费用未知`;return `估算¥${(c?.known_estimate_cny||0).toFixed(4)}`;}
  function render(batch){
    state.busy=batch.status==='RUNNING';el('batch-result').hidden=false;
    const c=batch.counts,done=batch.items.length-(c.RUNNING||0)-(c.QUEUED||0);
    el('batch-state').textContent=`${names[batch.status]||batch.status} · ${done}/${batch.items.length}份已结束，成功执行${c.PARTIAL}份、失败${c.FAILED}份、重复跳过${c.SKIPPED}份。${batch.message}`;
    el('batch-progress').max=batch.items.length||1;el('batch-progress').value=done;
    el('batch-usage').textContent=`${usage(batch.usage)} · ${cost(batch.cost,batch.usage)}。费用为按填写单价的估算，以阿里账单为准。成功执行不等于B审核通过。`;
    el('batch-export').hidden=state.busy||!batch.export_available;el('batch-export').href=`/api/pipeline/batches/${batch.batch_id}/export.xlsx`;
    el('batch-receipt').href=`/api/pipeline/batches/${batch.batch_id}/receipt.json`;
    const body=el('batch-results-body');body.replaceChildren();
    batch.items.forEach(row=>{
      const tr=node('tr');tr.append(node('td',`${row.name} · ${row.request.company_code||'自动识别'} · ${row.request.year}`));
      tr.append(node('td',`${names[row.status]||row.status}${row.stage&&row.stage!=='FINISHED'?' · '+(names[row.stage]||row.stage):''}`));
      const td=node('td');td.append(node('div',usage(row.usage)),node('div',cost(row.cost,row.usage),'form-help'));tr.append(td);
      tr.append(node('td',`${row.error_code?row.error_code+'：':''}${row.message||''}`));
      const result=node('td');if(row.result_run_id){const b=node('button','查看结果','button secondary');b.type='button';b.onclick=()=>wb.selectRun(row.result_run_id).catch(e=>error(e.message));result.append(b);}tr.append(result);body.append(tr);
    });controls();
  }
  async function select(identity){
    state.selected=identity;clearTimeout(state.timer);
    try{
      const batch=await wb.api(`/api/pipeline/batches/${identity}`);if(state.selected!==identity)return;
      render(batch);
      if(state.busy)state.timer=setTimeout(()=>select(identity),1500);else await history();
    }catch(e){error('队列状态暂时无法读取：'+e.message+'。后台不会因此重复提交。刷新页面可重新连接。');state.timer=setTimeout(()=>select(identity),5000);}
  }
  async function history(){
    const data=await wb.api('/api/pipeline/batches'),body=el('batch-history');body.replaceChildren();
    data.batches.forEach(batch=>{const b=node('button',`${new Date(batch.created_at).toLocaleString('zh-CN')} · ${batch.items.length}份 · ${names[batch.status]||batch.status}`,'button secondary');b.type='button';b.disabled=state.busy&&batch.batch_id!==state.selected;b.onclick=()=>select(batch.batch_id);body.append(b);});return data.batches;
  }
  el('batch-form').onsubmit=async e=>{
    e.preventDefault();error('');const model=el('batch-mode').value==='model';
    if(model&&!el('batch-consent').checked){error('请确认本批全部报告的外发范围与调用上限。');return;}
    const pricing={};for(const key of ['extract_input','extract_output','audit_input','audit_output']){const value=el('batch-'+key.replace('_','-')).value;pricing[key]=value===''?null:Number(value);}
    const body={items:state.draft.filter(x=>x.id).map(x=>({source_id:x.id,name:x.name,company_code:x.company_code||null,year:Number(x.year)})),
      options:{mode:model?'model':'rules_only',allow_external:model&&el('batch-consent').checked,a_calls:model?Number(el('batch-a-calls').value):0,b_calls:model?Number(el('batch-b-calls').value):0},pricing,price_note:el('batch-price-note').value};
    state.busy=true;controls();
    try{const value=await wb.api('/api/pipeline/batches',body);el('batch-consent').checked=false;await select(value.batch_id);}
    catch(e){state.busy=false;controls();error(e.message);}
  };
  el('batch-cancel').onclick=async()=>{try{await wb.api(`/api/pipeline/batches/${state.selected}/cancel`,{});await select(state.selected);}catch(e){error(e.message);}};
  wb.ready.then(async data=>{
    if(!data)return;state.available=!!data.pipeline?.available;
    el('batch-mode').querySelector('option[value="model"]').disabled=!data.pipeline?.api_key_configured;
    state.reports=(await wb.api('/api/pipeline/reports')).reports;
    state.reports.forEach(r=>{const o=node('option',r.name);o.value=r.id;el('batch-existing').append(o);});
    const batches=await history(),active=batches.find(b=>b.status==='RUNNING');
    if(active||batches[0])await select((active||batches[0]).batch_id);controls();
  }).catch(e=>error(e.message));
})();
