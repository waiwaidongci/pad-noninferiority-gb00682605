import {display,esc} from '../../utils/html.js';
import {table} from './table.js';

function chart(spec) {
  const points = (spec?.points || []).filter(point => Number.isFinite(point.value)).slice(0,12);
  if (!points.length) return '';
  const max = Math.max(...points.map(point => Math.abs(point.value)), 1e-9);
  return `<figure><figcaption>${esc(spec.label)} ${spec.unit ? `· ${esc(spec.unit)}` : ''}</figcaption>${points.map(point => {
    const proportion = Math.abs(point.value) / max * 47;
    const start = point.value < 0 ? 50 - proportion : 50;
    return `<div class="chart-row"><span>${esc(point.label)}</span><div class="chart-track"><i style="inset-inline-start:${start}%;width:${proportion}%"></i></div><strong>${esc(display(point.value))}</strong></div>`;
  }).join('')}</figure>`;
}

function verdictBadge(plan) {
  if (plan.can_publish) return '<span class="badge badge-pass">可提交复核</span>';
  return '<span class="badge badge-block">不可发布</span>';
}

function pairingPlans(result) {
  const plans = result.pairing_plans || result.alternative_pairings || [];
  if (!plans.length) return '';
  const conflict = result.tie_conflict;
  return `<section class="pairing-review ${conflict ? 'is-conflict' : ''}">
    <h3>并列配法核对（两套配对）</h3>
    ${conflict ? '<p class="tie-banner" role="alert">两套并列配对按原验收标准会得出相反判定，本批已暂缓；下面列出这两套配对，请逐站核对重叠窗口与原始读数。</p>'
               : '<p class="hint">各站最久重叠存在并列时形成两套配对；两套判定一致，仍全部留档。</p>'}
    <div class="pairing-grid">
      ${plans.map((plan, index) => `<article class="pairing-plan">
        <h4>${esc(plan.label || `配对 ${index + 1}`)} ${verdictBadge(plan)}</h4>
        <p class="hint">${esc(plan.headline || '')}<br>有效站数 ${esc(plan.station_count ?? plan.rows.length)} · 单侧95%上界 ${plan.upper == null ? '无法计算' : esc(display(plan.upper))}</p>
        ${table(plan.rows)}
      </article>`).join('')}
    </div>
  </section>`;
}

export function report(result) {
  const metrics = (result.metric_order || Object.keys(result.metrics)).map(key => [key,result.metrics[key]]);
  const columns = result.columns?.map(key => ({key,label:key}));
  const pending = result.tie_conflict ? '<p class="tie-banner" role="alert">验收状态：暂缓（并列配法可能给出相反判定），不可批准或分发。</p>' : '';
  return `<div class="reading"><h2>${esc(result.headline)}</h2>${pending}<dl class="metrics">${metrics.map(([key,value]) => `<div><dt>${esc(key)}</dt><dd>${esc(display(value))}</dd></div>`).join('')}</dl>${chart(result.chart)}${pairingPlans(result)}<h3>业务明细（主配对）</h3>${table(result.rows,columns)}<h3>口径与限制</h3><ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul></div>`;
}
