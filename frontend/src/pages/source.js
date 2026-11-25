import {api, download, request} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {actions} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc,time} from '../utils/html.js';

const DEFAULT_BATCH = 'PAD-SHIFT-CURRENT';

export async function overview() {
  const profile = state.profile;
  const history = await api('/audit');
  return `<section class="summary"><h2>${esc(profile.organization)}</h2><p>${esc(profile.context)}</p><ol>${profile.scenario.map(step => `<li>${esc(step)}</li>`).join('')}</ol><a class="button" href="#1">核对数据</a></section><section><h2>最近操作</h2>${table(history.map(row => ({时间:time(row.at),账号:row.actor,操作:actions[row.action] ?? row.action,记录:row.subject})))}</section>`;
}

function intakeStatusTable(intake) {
  const batches = intake?.batches || [];
  if (!batches.length) return '<p class="hint">还没有分开交表记录。两班分别上传后，这里会显示到齐状态。</p>';
  return table(batches.map(batch => ({
    交接批: batch.batch_code,
    原垫班: batch.baseline ? `${batch.baseline.filename} · ${batch.baseline.rows_count} 行` : '未交',
    试装垫班: batch.trial ? `${batch.trial.filename} · ${batch.trial.rows_count} 行` : '未交',
    状态: batch.complete ? '两张表已到齐，已生成可分析版本' : `等待${!batch.baseline ? '原垫班' : '试装垫班'}交表`,
    最近交表: time(batch.last_created),
  })));
}

function intakeCards(profile) {
  const rule = profile.intake?.rule || '原垫班和试装垫班分开交表，两张表到齐才生成可分析版本。';
  return `<section class="intake-grid" aria-label="两班交表">
    <label class="intake-card" style="grid-column:1/-1">交接批号（两班需一致）<input id="intake-batch" type="text" value="${esc(DEFAULT_BATCH)}" maxlength="80"></label>
    <div class="intake-card">
      <h3>原垫班测量表</h3><p class="hint">一站可有多条窗口记录，需含站号、窗口起止（或时长）与原垫热损。</p>
      <form id="intake-baseline" class="inline-form"><label>选择原垫 CSV<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">交原垫班表</button>${errorSlot}</form>
    </div>
    <div class="intake-card">
      <h3>试装垫班测量表</h3><p class="hint">一站可有多条窗口记录，需含站号、窗口起止（或时长）与试装热损。</p>
      <form id="intake-trial" class="inline-form"><label>选择试装 CSV<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">交试装垫班表</button>${errorSlot}</form>
    </div>
  </section>
  <p class="hint">${esc(rule)}</p>`;
}

export async function source() {
  const profile = state.profile;
  const [revisions, intake] = await Promise.all([api('/datasets'), api('/intake')]);
  const active = revisions.find(row => row.id === state.datasetId) || revisions[0];
  const detail = active ? await api(`/datasets/${active.id}`) : null;
  const analyst = state.account.role === 'analyst';
  const shiftSection = `<section><h2>两班分开交表</h2>
    ${analyst ? intakeCards(profile) : '<p class="hint">分析员可分别上传原垫班与试装垫班测量表；其他角色可查看两班到齐状态并下载模板。</p>'}
    <div class="sample-actions">
      ${analyst ? `<button type="button" id="sample-standard" class="secondary">一键交齐标准两班样例（36 站，应通过）</button>
      <button type="button" id="sample-tie" class="secondary">一键交齐并列冲突样例（30 站，暂缓）</button>` : ''}
      <button type="button" id="example-baseline" class="secondary">下载原垫表模板</button>
      <button type="button" id="example-trial" class="secondary">下载试装表模板</button>
    </div>${analyst ? '<p id="sample-error" role="alert" class="error"></p>' : ''}
    <h3>交接批到齐状态</h3>${intakeStatusTable(intake)}
  </section>`;
  const legacy = `<section><details>
    <summary><h2 style="display:inline">旧版已配对台账 CSV（六列格式，保留兼容）</h2></summary>
    <p class="hint">CSV 表头顺序可调整，列名需匹配样例。</p>
    ${analyst ? `<form id="upload" class="inline-form"><label>导入已配对数据文件<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">保存数据版本</button>${errorSlot}</form>` : ''}
    <button type="button" id="example" class="secondary">下载旧版配对样例 CSV</button>
    <p id="download-error" class="error"></p>
  </details></section>`;
  return `${shiftSection}${legacy}
  ${detail ? `<section><label>数据版本<select id="revision">${revisions.map(row => `<option value="${row.id}" ${row.id === active.id ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}</select></label><p class="hint">${detail.count} 行 · 页面预览前 ${detail.records.length} 行 · SHA-256 ${esc(detail.checksum.slice(0,12))}</p>${table(detail.records,profile.columns)}</section>` : empty('请先交齐两班测量表，或导入项目样例 CSV。')}`;
}

async function submitIntake(root, kind, form) {
  const file = form.elements.file.files[0];
  const batch_code = root.querySelector('#intake-batch')?.value?.trim() || DEFAULT_BATCH;
  const imported = await api(`/intake/${kind}`, {batch_code, name: file.name, csv: await file.text()});
  const assembly = imported.assembly || {};
  if (assembly.dataset_id) {
    state.datasetId = assembly.dataset_id;
    state.status = `两张表已到齐，生成数据版本 #${assembly.dataset_id}：配对 ${assembly.paired_station_count} 站，并列 ${assembly.tie_count} 站。可到「非劣效检验」提交计算。`;
  } else {
    state.status = `${kind === 'baseline' ? '原垫班' : '试装垫班'}表已收（交接批 ${batch_code}），还在等待${(assembly.waiting_for || []).join('、')}，到齐后才生成可分析版本。`;
  }
}

async function loadSamplePair(root, kind) {
  const errorSlot = root.querySelector('#sample-error');
  if (errorSlot) errorSlot.textContent = '';
  try {
    const tie = kind === 'tie';
    const [baseResp, trialResp] = await Promise.all([
      request(tie ? '/example-tie-baseline.csv' : '/example-baseline.csv'),
      request(tie ? '/example-tie-trial.csv' : '/example-trial.csv'),
    ]);
    const [baseline_csv, trial_csv] = await Promise.all([baseResp.text(), trialResp.text()]);
    const saved = await api('/datasets/pair-tables', {
      name: tie ? '并列配法相反判定样例（30 站）' : '两班标准样例（36 站）',
      baseline_csv, trial_csv,
    });
    state.datasetId = saved.id;
    state.status = `已生成两班配对版本 #${saved.id}：配对 ${saved.paired_stations} 站，并列 ${saved.tie_count} 站${tie ? '；该样例两套判定相反，应暂缓' : ''}。`;
    location.hash = '#2';
  } catch (error) {
    if (errorSlot) errorSlot.textContent = error.message;
  }
}

export function bindSource(root, refresh) {
  bindForm(root, '#upload', async form => {
    const file = form.elements.file.files[0];
    const imported = await api('/datasets', {name:file.name,csv:await file.text()});
    state.datasetId = imported.id;
    state.status = imported.created ? `已保存 ${imported.rows} 行。` : '数据与已有版本一致，已选中该版本。';
  }, refresh);
  bindForm(root, '#intake-baseline', form => submitIntake(root, 'baseline', form), refresh);
  bindForm(root, '#intake-trial', form => submitIntake(root, 'trial', form), refresh);

  root.querySelector('#revision')?.addEventListener('change', event => {
    state.datasetId = Number(event.target.value);
    refresh();
  });
  root.querySelector('#example')?.addEventListener('click', async () => {
    try {await download('/example.csv', state.profile.filename);}
    catch (error) {root.querySelector('#download-error').textContent = error.message;}
  });
  root.querySelector('#example-baseline')?.addEventListener('click', () => download('/example-baseline.csv', state.profile.baseline_filename || 'baseline.csv'));
  root.querySelector('#example-trial')?.addEventListener('click', () => download('/example-trial.csv', state.profile.trial_filename || 'trial.csv'));
  root.querySelector('#sample-standard')?.addEventListener('click', () => loadSamplePair(root, 'standard'));
  root.querySelector('#sample-tie')?.addEventListener('click', () => loadSamplePair(root, 'tie'));
}
