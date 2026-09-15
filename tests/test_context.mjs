import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {boundContext, boundPayload} from '../akari_context.mjs';

test('12 historical images become 2, originals and tool pairs remain recoverable', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'akari-context-'));
  try {
    const messages = [{role:'user', content:[{type:'text', text:'下载、合并并发送视频'}]}];
    for (let i=0; i<12; i++) {
      messages.push({role:'assistant', content:[{type:'toolCall',id:'t'+i,name:'read',arguments:{path:'frame'+i}}]});
      messages.push({role:'toolResult',toolCallId:'t'+i,content:[
        {type:'image',data:Buffer.from('image'+i).toString('base64'),mimeType:'image/png'},
        {type:'text',text:'网页内容'.repeat(30000)+'末尾重要结果'}]});
    }
    const original = JSON.stringify(messages);
    const result = boundContext(messages,root);
    assert.equal(result.flatMap(m=>m.content).filter(b=>b.type==='image').length,2);
    assert.equal(result.length,messages.length);
    assert.equal(JSON.stringify(messages),original);
    assert.equal(fs.readdirSync(root).filter(f=>f.endsWith('.png')).length,12);
    assert.ok(fs.readdirSync(root).some(f=>f.endsWith('.txt') && fs.readFileSync(path.join(root,f),'utf8').endsWith('末尾重要结果')));
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('wire payload bounds include tools and UTF8 text while preserving matched tool pairs', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'akari-wire-'));
  try {
    const payload = {max_tokens:65536,messages:[{role:'user',content:'原始要求'}]};
    for(let i=0;i<12;i++) {
      payload.messages.push({role:'assistant',content:null,tool_calls:[{id:'t'+i,type:'function',function:{name:'read',arguments:'{}'}}]});
      payload.messages.push({role:'tool',tool_call_id:'t'+i,content:'长网页'.repeat(20000)});
      payload.messages.push({role:'user',content:[{type:'image_url',image_url:{url:'data:image/png;base64,abc'}}]});
    }
    const result = boundPayload(payload,root);
    assert.equal(result.max_tokens,16384);
    assert.ok(result.messages.some(m=>m.content==='原始要求'));
    const stats = JSON.parse(fs.readFileSync(path.join(root,'budget.jsonl'),'utf8').trim());
    assert.ok(stats.textBytes<=196608);
    assert.equal(stats.images,2);
    assert.ok(stats.textBytes+2*16384+16384<262144);
    const calls = result.messages.flatMap(m=>m.tool_calls || []).map(c=>c.id).sort();
    const replies = result.messages.filter(m=>m.role==='tool').map(m=>m.tool_call_id).sort();
    assert.deepEqual(calls,replies);
    assert.ok(stats.archivedExchanges > 0);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('200 successive steps remain bounded, newest results and all user constraints survive', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'akari-rolling-'));
  try {
    const payload = {messages:[{role:'system',content:'s'.repeat(105000)},{role:'user',content:'保留原文件，不重复外部操作'}]};
    for(let i=0;i<200;i++) {
      payload.messages.push({role:'assistant',tool_calls:[{id:'step'+i,type:'function',function:{name:'bash',arguments:JSON.stringify({command:'echo '+ 'x'.repeat(1800)})}}]});
      payload.messages.push({role:'tool',tool_call_id:'step'+i,content:'result-'+i+'\n'+'z'.repeat(2500)});
      if(i===90) payload.messages.push({role:'user',content:'新增约束：不要改变编码'});
      const result = boundPayload(payload,root);
      assert.ok(Buffer.byteLength(JSON.stringify(result),'utf8') <=196608);
      assert.ok(result.messages.some(m=>m.tool_call_id==='step'+i));
      assert.ok(result.messages.some(m=>m.content==='保留原文件，不重复外部操作'));
      if(i>=90) assert.ok(result.messages.some(m=>m.content==='新增约束：不要改变编码'));
      assert.deepEqual(result.messages.flatMap(m=>m.tool_calls||[]).map(c=>c.id).sort(),
        result.messages.filter(m=>m.role==='tool').map(m=>m.tool_call_id).sort());
    }
    const records = Object.values(JSON.parse(fs.readFileSync(path.join(root,'steps-index.json'),'utf8')));
    const first = records.find(r=>r.toolCallIds.includes('step0'));
    const original = JSON.parse(fs.readFileSync(first.file,'utf8'));
    assert.equal(original[0].tool_calls[0].function.arguments,payload.messages[2].tool_calls[0].function.arguments);
    assert.equal(original[1].content,payload.messages[3].content);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('partial parallel batches remain intact and completed huge arguments can be archived', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'akari-pairs-'));
  try {
    const call = id => ({id,type:'function',function:{name:'bash',arguments:JSON.stringify({command:'x'.repeat(150000)})}});
    const incomplete = {messages:[{role:'user',content:'task'}, {role:'assistant',tool_calls:[call('a'),call('b')]},
      {role:'tool',tool_call_id:'a',content:'done'}]};
    assert.throws(()=>boundPayload(incomplete,root), /AKARI_CONTEXT_BUDGET/);
    assert.ok(!fs.existsSync(path.join(root,'steps-index.json')));
    const complete = structuredClone(incomplete);
    complete.messages.push({role:'tool',tool_call_id:'b',content:'also done'});
    const result = boundPayload(complete,root);
    assert.ok(Buffer.byteLength(JSON.stringify(result)) <196608);
    assert.equal(result.messages.filter(m=>m.tool_calls).length,0);
    const entries = Object.values(JSON.parse(fs.readFileSync(path.join(root,'steps-index.json'),'utf8')));
    assert.deepEqual(entries[0].toolCallIds,['a','b']);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('irreducibly oversized instructions fail explicitly instead of silently dropping the user request', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'akari-bound-'));
  try {
    assert.throws(()=>boundPayload({messages:[{role:'user',content:'x'.repeat(300000)}]},root), /AKARI_CONTEXT_BUDGET/);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('a new QQ job archives previous complete tool exchanges and retains a readable handoff', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'akari-handoff-'));
  try {
    const messages = [{role:'user',content:[{type:'text',text:'旧任务要求\n【QQ 任务交付约定】旧目录 /tmp/old'}]}];
    for(let i=0;i<60;i++) {
      messages.push({role:'assistant',content:[{type:'toolCall',id:'old'+i,name:'bash',arguments:{command:'echo '+ 'x'.repeat(1500)}}]});
      messages.push({role:'toolResult',toolCallId:'old'+i,content:[{type:'text',text:'old output '+i}]});
    }
    messages.push({role:'assistant',content:[{type:'text',text:'旧结果在 /tmp/old/result.md'}]});
    const next = {role:'user',content:[{type:'text',text:'当前任务原文\n【QQ 任务交付约定】新目录 /tmp/new'}]};
    messages.push(next);
    messages.push({role:'assistant',content:[{type:'toolCall',id:'current',name:'read',arguments:{path:'/tmp/new/input'}}]});
    messages.push({role:'toolResult',toolCallId:'current',content:[{type:'text',text:'current output'}]});
    const original = JSON.stringify(messages);
    const result = boundContext(messages,root);
    assert.equal(result.length,4);
    assert.deepEqual(result[1],next);
    assert.match(result[0].content[0].text,/旧任务要求/);
    assert.match(result[0].content[0].text,/\/tmp\/old\/result.md/);
    assert.equal(result[2].content[0].id,'current');
    assert.equal(result[3].toolCallId,'current');
    const archive = fs.readdirSync(root).find(file => file.endsWith('.json'));
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root,archive),'utf8')),messages.slice(0,-3));
    assert.equal(JSON.stringify(messages),original);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
