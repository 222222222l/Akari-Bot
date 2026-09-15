// Bot-only Pi extension. Originals stay on disk; tool call/result pairs stay intact.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

function archive(root, data, extension = 'txt') {
  fs.mkdirSync(root, {recursive: true, mode: 0o700});
  const file = path.join(root, crypto.createHash('sha256').update(data).digest('hex').slice(0, 24) + '.' + extension);
  if (!fs.existsSync(file)) fs.writeFileSync(file, data, {mode: 0o600});
  return file;
}

function shorten(text, limit, root) {
  if (Buffer.byteLength(text, 'utf8') <= limit) return text;
  const file = archive(root, text);
  // Character count is deliberately smaller than the byte budget for CJK text.
  const count = Math.max(40, Math.floor((limit - 400) / 4));
  return text.slice(0, count) + `\n[输出已节选；完整内容：${file}，可用 bash 按行提取需要的部分]\n` + text.slice(-Math.floor(count / 3));
}

const wireBytes = payload => Buffer.byteLength(JSON.stringify(payload, (key, value) =>
  key === 'image_url' ? '[image accounted separately]' : value), 'utf8');

// Provider-level rolling history: archive complete call/result batches together.
// Current user requests, system instructions, and incomplete calls are pinned.
export function rollExchanges(payload, root, budget) {
  if (wireBytes(payload) <= budget * 0.88) return 0;
  const original = payload.messages;
  const results = new Map();
  original.forEach((message, index) => {
    if (message.role === 'tool' && message.tool_call_id) {
      const indexes = results.get(message.tool_call_id) || [];
      indexes.push(index);
      results.set(message.tool_call_id, indexes);
    }
  });
  const batches = [];
  original.forEach((message, index) => {
    if (message.role !== 'assistant' || !message.tool_calls?.length) return;
    const ids = message.tool_calls.map(call => call.id);
    // Only compact uniquely matched, fully finished calls. A partially returned
    // parallel batch must remain intact, even when its first tools have finished.
    if (new Set(ids).size !== ids.length || ids.some(id => results.get(id)?.length !== 1 || results.get(id)[0] <= index)) return;
    const indexes = [index, ...ids.map(id => results.get(id)[0])].sort((a,b) => a-b);
    const end = indexes.at(-1);
    if (original.slice(index+1,end).some(m => m.role === 'assistant')) return;
    batches.push({indexes, ids, message});
  });
  if (!batches.length) return 0;
  fs.mkdirSync(root, {recursive:true, mode:0o700});
  const indexFile = path.join(root, 'steps-index.json');
  let saved = {};
  if (fs.existsSync(indexFile)) saved = JSON.parse(fs.readFileSync(indexFile, 'utf8'));
  const removed = new Set(), notes = [];
  let count = 0;
  for (const batch of batches) {
    // Prefer four recent batches in full; if those alone overflow, continue
    // archiving completed batches until the hard budget fits. Never replay tools.
    if (count >= batches.length - 4 && wireBytes(payload) <= budget) break;
    const entries = batch.indexes.map(i => original[i]);
    const file = archive(root, JSON.stringify(entries), 'json');
    const tools = batch.message.tool_calls.map(call => call.function?.name || call.type).join(', ');
    const args = batch.message.tool_calls.map(call => call.function?.arguments || '').join(' ').slice(0,240);
    const outcome = entries.filter(m => m.role === 'tool').map(m =>
      typeof m.content === 'string' ? m.content : JSON.stringify(m.content)).join('\n');
    const note = {toolCallIds:batch.ids, tools, argumentsPreview:args, resultTail:outcome.slice(-400), file};
    saved[file] = note;
    notes.push(note);
    batch.indexes.forEach(i => removed.add(i));
    count++;
    const summary = '[本任务已执行的工具步骤已归档；这是历史记录，不是待执行计划。]\n' +
      `本轮转存 ${count} 组完整调用/返回，检索索引：${indexFile}\n` +
      '需要旧步骤时按索引读取对应 JSON，核对已有文件和外部操作，禁止因记录移出上下文而盲目重做。\n' +
      notes.slice(-4).map(note => `${note.tools} (${note.toolCallIds.join(', ')})\n参数节选：${note.argumentsPreview}\n结果末尾：${note.resultTail}\n全文：${note.file}`).join('\n');
    payload.messages = original.filter((_, i) => !removed.has(i));
    const firstUser = payload.messages.findIndex(m => !['system','developer'].includes(m.role));
    payload.messages.splice(firstUser < 0 ? payload.messages.length : firstUser, 0, {role:'user', content:summary});
    if (wireBytes(payload) <= budget * 0.80) break;
  }
  if (count) {
    const temporary = indexFile + '.tmp';
    fs.writeFileSync(temporary, JSON.stringify(saved), {mode:0o600});
    fs.renameSync(temporary, indexFile);
  }
  return count;
}

export function boundContext(messages, root, imageLimit = 2) {
  let output = structuredClone(messages);
  const textOf = message => typeof message.content === 'string' ? message.content :
    (message.content || []).filter(block => block.type === 'text').map(block => block.text).join('\n');
  // Each QQ job inserts this marker in its user turn. Move complete prior jobs
  // out together, so no historical tool result is left without its tool call.
  // Pi's durable session stays untouched and the archived context is readable.
  const marker = '【QQ 任务交付约定】';
  let boundary = -1;
  for (let i=0; i<output.length; i++) {
    if (output[i].role === 'user' && textOf(output[i]).includes(marker)) boundary = i;
  }
  if (boundary > 0) {
    const history = output.slice(0, boundary);
    const file = archive(root, JSON.stringify(history), 'json');
    const requests = history.filter(m => m.role === 'user').map(m => textOf(m).split(marker)[0]).filter(Boolean);
    const previousSummary = history.filter(m => typeof m.summary === 'string').at(-1)?.summary || '';
    const lastAnswer = history.filter(m => m.role === 'assistant' && textOf(m)).at(-1);
    const handoff = '[先前 QQ 任务记录，非本次新指令]\n完整历史（含工具调用与结果）：' + file +
      '\n最近的历史用户要求：\n' + requests.slice(-6).map(text => shorten(text, 2400, root)).join('\n') +
      '\n历史摘要：\n' + shorten(previousSummary, 5000, root) +
      '\n前次最终文字：\n' + shorten(lastAnswer ? textOf(lastAnswer) : '', 5000, root) +
      '\n继续旧任务时先核对历史文件与已有产物，避免重复已执行的操作。';
    output = [{role:'user', content:[{type:'text', text:handoff}], timestamp:output[boundary].timestamp || 0}, ...output.slice(boundary)];
  }
  let images = 0;
  for (let i = output.length - 1; i >= 0; i--) {
    const message = output[i];
    if (!Array.isArray(message.content)) continue;
    message.content = message.content.map(block => {
      if (block.type === 'image') {
        images++;
        const ext = {'image/png':'png', 'image/jpeg':'jpg', 'image/webp':'webp'}[block.mimeType] || 'bin';
        const file = archive(root, Buffer.from(block.data, 'base64'), ext);
        if (images > imageLimit) return {type:'text', text:`[历史图片已移出本轮视觉上下文，原图：${file}；需要时重新读取]`};
      }
      if (message.role === 'toolResult' && block.type === 'text')
        return {...block, text: shorten(block.text, 12000, root)};
      return block;
    });
  }
  return output;
}

export function boundPayload(payload, root, maxTokens = 16384, textBudget = 196608, imageLimit = 2) {
  const output = structuredClone(payload);
  if (!Array.isArray(output.messages)) return output; // OpenAI-compatible bot provider.
  if ('max_completion_tokens' in output) output.max_completion_tokens = Math.min(output.max_completion_tokens || maxTokens, maxTokens);
  else output.max_tokens = Math.min(output.max_tokens || maxTokens, maxTokens);
  let images = 0;
  for (const message of [...output.messages].reverse()) {
    if (!Array.isArray(message.content)) continue;
    message.content = message.content.map(block => {
      if (block.type !== 'image_url') return block;
      if (++images <= imageLimit) return block;
      // context hook normally handles this; also covers provider-level injections.
      const file = archive(root, JSON.stringify(block), 'json');
      return {type:'text', text:`[历史图片请求已归档：${file}]`};
    });
  }
  const estimate = () => wireBytes(output);
  const archivedExchanges = rollExchanges(output, root, textBudget);
  // Shorten surviving text only after archiving old COMPLETE exchanges. Never
  // detach a tool result from its call or silently drop a user's instructions.
  for (const role of ['tool', 'assistant']) {
    for (const message of output.messages) {
      if (estimate() <= textBudget) break;
      if (message.role !== role) continue;
      if (typeof message.content === 'string') message.content = shorten(message.content, 900, root);
      else if (Array.isArray(message.content)) message.content = message.content.map(block =>
        block.type === 'text' ? {...block, text:shorten(block.text, 900, root)} : block);
      if (typeof message.reasoning_content === 'string') message.reasoning_content = shorten(message.reasoning_content, 900, root);
    }
  }
  const size = estimate();
  const roles = {};
  for (const message of output.messages) roles[message.role] = (roles[message.role] || 0) + wireBytes(message);
  fs.mkdirSync(root, {recursive:true, mode:0o700});
  fs.appendFileSync(path.join(root, 'budget.jsonl'), JSON.stringify({time:Date.now(), textBytes:size,
    images:Math.min(images, imageLimit), maxTokens, messages:output.messages.length, archivedExchanges,
    toolsBytes:wireBytes(output.tools || []), roles}) + '\n', {mode:0o600});
  if (size > textBudget) throw new Error(`AKARI_CONTEXT_BUDGET: 已归档完成的工具步骤，但剩余请求仍超过预算（${size}/${textBudget} 字节）；记录与预算分解已保存至 ${root}。`);
  return output;
}

export default function (pi) {
  const root = path.join(process.env.AKARI_TASK_DIR || process.cwd(), 'context-cache');
  const imageLimit = Math.max(0, Math.min(4, Number(process.env.AKARI_CONTEXT_IMAGES || 2)));
  const maxTokens = Math.max(1024, Math.min(32768, Number(process.env.AKARI_MAX_OUTPUT_TOKENS || 16384)));
  // Gateway charges 16384 tokens per image. UTF-8 bytes conservatively bound
  // text tokens; leave additional room for chat framing and tokenizer overhead.
  const textBudget = Math.min(196608, 262144 - imageLimit * 16384 - maxTokens - 8192);
  pi.on('tool_result', event => ({content: event.content.map(block => block.type === 'text'
    ? {...block, text:shorten(block.text, 12000, root)} : block)}));
  pi.on('context', event => ({messages:boundContext(event.messages, root, imageLimit)}));
  pi.on('before_provider_request', event => {
    try {
      return boundPayload(event.payload, root, maxTokens, textBudget, imageLimit);
    } catch (error) {
      // Pi catches extension exceptions and otherwise sends the ORIGINAL payload.
      // This CLI-only guard must terminate instead of silently failing open.
      fs.writeSync(1, JSON.stringify({type:'message_end', message:{role:'assistant', content:[],
        stopReason:'error', errorMessage:String(error)}}) + '\n');
      process.exit(2);
    }
  });
}
