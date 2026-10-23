/* The editor and test scripts operate on the same per-node request files. */
(() => {
  'use strict';
  const $ = id => document.getElementById('node-request-' + id);
  let loaded = null, rows = [], dirty = false, busy = false;
  const message = value => { $('message').textContent = value; };
  function controls() {
    $('save').disabled = !loaded || busy;
    $('run').disabled = !loaded || dirty || busy;
    for (const id of ['load', 'refresh', 'select', 'node']) $(id).disabled = busy;
    $('json').disabled = !loaded || busy;
  }
  async function api(url, body) {
    const response = await fetch(url, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const value = await response.json();
    if (!response.ok) throw Error(value.error || '请求失败');
    return value;
  }
  const guard = fn => async () => {
    try { await fn(); } catch (error) { message(error.message); }
    finally { controls(); }
  };
  function options() {
    const selected = rows.filter(row => row.node === $('node').value);
    $('select').replaceChildren(new Option('选择请求', ''),
      ...selected.map(row => new Option(row.id, row.id)));
    if (loaded && loaded.node === $('node').value) $('select').value = loaded.id;
  }
  async function history() {
    if (!loaded) return;
    const runs = (await api(`/api/requests/${loaded.node}/${loaded.id}/history`)).runs;
    $('history').replaceChildren(...runs.map(run => {
      const row = document.createElement('li');
      row.textContent = `${run.run_id} · ${run.status} ${run.error || ''} `;
      const base = `/resource/output/${run.node}/${run.id}/${run.run_id}/`;
      for (const [file, label] of [['source_request.json', '原始请求'], ['run.json', '运行信息'], ...(run.status === 'DONE' ? [['request.json', '实际输入'], ['response.json', '结果']] : [])]) {
        const link = document.createElement('a'); link.href = base + file;
        link.target = '_blank'; link.textContent = label + ' '; row.append(link);
      }
      return row;
    }));
  }
  async function refresh() {
    const data = await api('/api/requests'); rows = data.requests;
    const previous = $('node').value;
    $('node').replaceChildren(...Object.keys(data.nodes).map(n => new Option(n, n)));
    if (previous) $('node').value = previous;
    options(); await history();
    if (!rows.length) message('尚无请求，请运行 request_genaration_test.py。');
  }
  $('refresh').onclick = guard(refresh);
  $('node').onchange = options;
  $('load').onclick = guard(async () => {
    const id = $('select').value;
    if (!id) throw Error('请先选择请求');
    if (dirty) throw Error('当前请求有未保存的修改，请先保存');
    loaded = await api(`/api/requests/${$('node').value}/${encodeURIComponent(id)}`);
    $('json').value = JSON.stringify(loaded.request, null, 2);
    $('panel').open = true; dirty = false;
    message('已加载：' + loaded.path); await history();
  });
  $('json').addEventListener('input', () => {
    dirty = true; message('已修改，请保存后运行。'); controls();
  });
  $('save').onclick = guard(async () => {
    loaded = await api('/api/requests/save', {
      node: loaded.node, id: loaded.id, revision: loaded.revision,
      request: JSON.parse($('json').value)
    });
    dirty = false; message('已保存：' + loaded.path);
  });
  $('run').onclick = guard(async () => {
    busy = true; controls();
    try {
      const job = await api('/api/requests/run', {node: loaded.node, id: loaded.id, revision: loaded.revision});
      for (;;) {
        const status = await api('/api/job/' + job.job_id);
        message(status.message + (status.error ? '：' + status.error : ''));
        $('log').textContent = (status.log || []).map(row => row.message).join('\n');
        if (status.status === 'DONE' || status.status === 'ERROR') break;
        await new Promise(resolve => setTimeout(resolve, 700));
      }
      await history();
    } finally { busy = false; }
  });
  if (location.protocol !== 'file:') guard(refresh)();
})();
