// Artifact-tool authoring adapter. The Python entry point discovers Node locally.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require = createRequire(import.meta.url);
const {FileBlob,SpreadsheetFile} = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const [input,output,yearText,preview] = process.argv.slice(2);
const data=JSON.parse(await fs.readFile(input,'utf8'));
const year=Number(yearText);
if(year!==2025)throw new Error('The supplied template only supports 2025');
const workbook=await SpreadsheetFile.importXlsx(await FileBlob.load(data.contract.template_path));
const sheet=workbook.worksheets.getItem('2025');
await fs.mkdir(path.dirname(output),{recursive:true});
if(preview){
  const before=await workbook.render({sheetName:'2025',range:'A1:O3',scale:1,format:'png'});
  await fs.writeFile(output+'.before.png',new Uint8Array(await before.arrayBuffer()));
}
const rows=data.rows.filter(r=>r.year===year).map(r=>data.contract.fields.map((f,i)=>{
  const v=r.values[f];
  if(v===null || v===undefined)return null;
  if(i===0)return String(v);
  if(i<4 || f==='revenue_currency')return String(v).startsWith('=')?"'"+v:String(v);
  const n=Number(v);if(!Number.isFinite(n))throw new Error('Nonfinite cell');return n;
}));
if(rows.length){
  sheet.getRange(`A2:D${rows.length+1}`).setNumberFormat('@');
  sheet.getRange(`F2:F${rows.length+1}`).setNumberFormat('@');
  sheet.getRange(`A2:O${rows.length+1}`).values=rows;
  // Numeric-looking identifiers remain strings in XLSX; the explicit display
  // pattern also prevents the preview renderer from dropping leading zeroes.
  sheet.getRange(`A2:A${rows.length+1}`).setNumberFormat('00000');
  sheet.getRange(`E2:E${rows.length+1}`).setNumberFormat('#,##0.00####');
  sheet.getRange(`G2:O${rows.length+1}`).setNumberFormat('#,##0.0#####');
  sheet.getRange(`A2:O${rows.length+1}`).format.rowHeight=48;
  sheet.getRange(`B2:D${rows.length+1}`).format.wrapText=true;
}
sheet.getRange('A:A').format.columnWidth=13;
sheet.getRange('B:B').format.columnWidth=42;
sheet.getRange('C:C').format.columnWidth=32;
sheet.getRange('D:D').format.columnWidth=38;
sheet.getRange('E:E').format.columnWidth=24;
sheet.getRange('F:F').format.columnWidth=14;
if(data.research_policy){
  sheet.getRange(`E2:E${rows.length+1}`).setNumberFormat('#,##0.000');
  sheet.getRange(`I2:I${rows.length+1}`).setNumberFormat('0.0000');
  const name=data.contract.rules_sheet_name || '字段对照与提取规则';
  const rules=workbook.worksheets.getItem(name);
  const notes=data.research_policy.notes.map(([label,text])=>[label,null,null,null,null,text]);
  rules.getRange(`A18:F${17+notes.length}`).values=notes;
  rules.getRange(`A18:F${17+notes.length}`).format.wrapText=true;
  rules.getRange(`A18:F${17+notes.length}`).format.rowHeight=48;
  rules.getRange(`A18:F${17+notes.length}`).format.fill='#FFF7E6';
}
workbook.recalculate();
if(preview){
  for(const [name,range] of [['identity','A1:F'+Math.min(rows.length+1,11)],['water','G1:O'+Math.min(rows.length+1,11)]]){
    const image=await workbook.render({sheetName:'2025',range,scale:1,format:'png'});
    await fs.writeFile(output+'.'+name+'.png',new Uint8Array(await image.arrayBuffer()));
  }
  await fs.writeFile(output+'.inspect.json',(await workbook.inspect({kind:'table',range:'2025!A1:O'+Math.min(rows.length+1,12),include:'values,formulas',tableMaxRows:12,tableMaxCols:15})).ndjson);
}
await (await SpreadsheetFile.exportXlsx(workbook)).save(output);
console.log(JSON.stringify({rows:rows.length,output}));
