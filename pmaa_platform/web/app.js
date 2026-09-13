/* PMAA 工作台 —— 原生 ES，无构建步骤。所有状态色与文案由后端结构化结果驱动。 */
const API = '/v1';
const state = { view: 'sample', analysisId: null, sampleId: null, preparationBatchId: null,
  methodId: null, components: [], refs: [], providers: [] };

/* ------------------------------------------------------------ 工具 */
const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function toast(msg, isErr) {
  let t = $('#toast');
  if (!t) { t = el('div', 'toast'); t.id = 'toast'; document.body.appendChild(t); }
  t.className = 'toast show' + (isErr ? ' err' : '');
  t.textContent = msg;
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 3600);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? (typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body)) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.message || data.detail || `HTTP ${res.status}`;
    const code = data.error_code ? `[${data.error_code}] ` : '';
    throw new Error(code + msg);
  }
  return data;
}

/* 状态 → 徽标样式 */
const LEVEL_CLASS = {
  confirmed: 'b-ok', probable: 'b-info', candidate: 'b-warn', unknown: 'b-mute',
  resolved: 'b-ok', ambiguous: 'b-warn', unsupported: 'b-mute',
  passed: 'b-ok', warning: 'b-warn', failed: 'b-danger', not_assessed: 'b-mute',
  calibrated_molar: 'b-ok', semiquantitative: 'b-info', area_only: 'b-warn', unavailable: 'b-mute',
  accepted: 'b-ok', needs_review: 'b-warn', rejected: 'b-danger',
  imported: 'b-ok', quarantined: 'b-warn', isolated: 'b-danger',
  locally_validated: 'b-ok', reference_only: 'b-info', unreviewed: 'b-mute',
};
const badge = v => `<span class="badge ${LEVEL_CLASS[v] || 'b-mute'}">${esc(v ?? '—')}</span>`;

/* ============================================================ 向导状态机
   七步串行：上一步的产出自动成为下一步的输入，未完成则下一步锁定。
   修改上游会把下游标记为「已过期」——不静默保留旧结论，也不能跳过重跑。
   参考库 / LLM 配置是旁路，随时可进，不参与门禁。
-------------------------------------------------------------------------- */
const STEPS = [
  { key: 'sample',     n: 1, title: '建档',       sub: '样品 · 方法 · 还原标签' },
  { key: 'import',     n: 2, title: '导入文件',   sub: '参考库 / 峰表 / 谱图文档' },
  { key: 'extract',    n: 3, title: '提取峰值',   sub: '轴校准 · 伪峰剔除 · 确认' },
  { key: 'peaks',      n: 4, title: '峰候选审核', sub: '逐峰接受 / 排除' },
  { key: 'match',      n: 5, title: '谱图比对',   sub: '参考库检索 · 证据清单' },
  { key: 'candidates', n: 6, title: '候选结构',   sub: 'PMAA 与来源残基' },
  { key: 'ai',         n: 7, title: 'AI 评议',    sub: '最相关项 · 理由 · 采纳' },
];
/* 每一步负责写入的数据键 */
const STEP_KEY = ['sample', 'file', 'peaks', 'review', 'analysis', 'candidates', 'assessment'];
const BYPASS = ['library', 'providers'];

const WIZ = { idx: 0, data: {}, done: {}, stale: {} };

/* 该步是否已产出可供下游使用的结果 */
function stepDone(i) {
  if (WIZ.stale[i]) return false;
  switch (i) {
    case 0: return !!(state.sampleId && state.methodId);
    case 1: return !!WIZ.data.file;
    case 2: return !!WIZ.data.peaks?.length;
    case 3: return !!WIZ.data.review?.length;
    case 4: return !!WIZ.data.analysis;
    case 5: return !!WIZ.data.candidates?.length;
    case 6: return !!WIZ.data.assessment;
    default: return false;
  }
}
/* 该步下一步被卡在哪一步（-1 表示通行） */
function firstBlocker(i) {
  for (let j = 0; j < i; j++) if (!stepDone(j)) return j;
  return -1;
}
const canEnter = i => i === 0 || firstBlocker(i) < 0;

/** 完成第 i 步：写入产出、解除过期、解锁下一步 */
function completeStep(i, payload) {
  const k = STEP_KEY[i];
  if (k) WIZ.data[k] = payload;
  WIZ.stale[i] = false;
  WIZ.done[i] = true;
  renderSteps(); renderWizBar();
}
/** 修改第 i 步 → 其之后所有结果作废，必须重跑 */
function invalidateFrom(i) {
  for (let j = i + 1; j < STEPS.length; j++) {
    const k = STEP_KEY[j];
    if (k) delete WIZ.data[k];
    WIZ.stale[j] = true; delete WIZ.done[j];
  }
  renderSteps(); renderWizBar();
}

function renderSteps() {
  const box = $('#stepsMain');
  if (!box) return;
  box.innerHTML = STEPS.map((s, i) => {
    const d = stepDone(i), lock = !canEnter(i);
    const cur = i === WIZ.idx && !BYPASS.includes(state.view);
    const cls = ['step', lock ? 'locked' : d ? 'ok' : '', cur ? 'active' : '',
                 WIZ.stale[i] ? 'stale' : ''].filter(Boolean).join(' ');
    return `<button class="${cls}" data-view="${s.key}" title="${esc(s.sub)}">
      <span class="sn">${d && !WIZ.stale[i] ? '✓' : s.n}</span>
      <span class="st"><b>${esc(s.title)}</b><i>${esc(s.sub)}</i></span></button>`;
  }).join('');
}

function renderWizBar() {
  const bar = $('#wizbar');
  if (!bar) return;
  if (BYPASS.includes(state.view)) { bar.classList.remove('show'); bar.innerHTML = ''; return; }
  const i = WIZ.idx, prev = i > 0 ? STEPS[i - 1] : null, next = i < STEPS.length - 1 ? STEPS[i + 1] : null;
  const ok = stepDone(i);
  bar.classList.add('show');
  bar.innerHTML = `
    <div class="wb-side">${prev ? `<button class="btn sec" id="wb_prev">← ${esc(prev.title)}</button>`
                                : '<span class="hint">流程起点</span>'}</div>
    <div class="wb-mid">第 <b>${STEPS[i].n}</b> / ${STEPS.length} 步 · ${esc(STEPS[i].title)}
      <span class="tag ${ok ? 'ok' : ''}">${ok ? '已完成' : '待完成'}</span>
      ${WIZ.stale[i] ? '<span class="tag warn">上游已改，需重跑</span>' : ''}</div>
    <div class="wb-side right">${next
      ? `<button class="btn" id="wb_next" ${ok ? '' : 'disabled'}>${esc(next.title)} →</button>`
      : `<button class="btn" id="wb_finish" ${ok ? '' : 'disabled'}>完成流程</button>`}</div>`;
  const p = $('#wb_prev'); if (p) p.onclick = () => go(prev.key);
  const n = $('#wb_next'); if (n) n.onclick = () => go(next.key);
  const f = $('#wb_finish'); if (f) f.onclick = () => viewFinish();
}

/* ------------------------------------------------------------ 视图路由 */
const VIEWS = { sample: viewSample, import: viewImport, extract: viewExtract,
  peaks: viewPeakReview, match: viewMatch, candidates: viewCandidates, ai: viewAI,
  library: viewLibrary, providers: viewProviders, ristd: viewRIStd };

function go(view) {
  const i = STEPS.findIndex(s => s.key === view);
  if (i >= 0) {
    const b = firstBlocker(i);
    if (b >= 0) { toast(`请先完成第 ${STEPS[b].n} 步「${STEPS[b].title}」`, true); return; }
    WIZ.idx = i;
  }
  state.view = view;
  renderSteps();
  VIEWS[view]?.();
  renderWizBar();
}

/* ============================================================ ① 样本建档 */
function viewSample() {
  $('#main').innerHTML = `
  <div class="card">
    <h2>① 样本建档</h2>
    <div id="s_gate"></div>
    <p class="hint">未测项目请留空，平台记为 unknown，<b>不会填 0</b>。含糖醛酸/硫酸基/氨基糖的样本会自动转入专项分支。</p>
    <div class="grid g3">
      <div class="field"><label>样本编号 *</label><input id="s_code" placeholder="如 PS-2026-001"></div>
      <div class="field"><label>来源</label><input id="s_source" placeholder="如 灵芝子实体"></div>
      <div class="field"><label>批次</label><input id="s_batch" placeholder="如 B1"></div>
      <div class="field"><label>干基质量 (mg)</label><input id="s_mass" type="number" step="0.01"></div>
      <div class="field"><label>单糖组成 (JSON)</label><input id="s_comp" placeholder='{"Glc":"major","Gal":"minor"}'></div>
      <div class="field"><label>已知修饰 (JSON)</label><input id="s_mod" placeholder='{"uronic_acid":"unknown"}'></div>
    </div>
    <div class="row"><button class="btn" id="s_save">建档</button></div>
    <div id="s_out"></div>
  </div>
  <div class="card"><h2>已登记样本</h2><div id="s_list" class="scroll"></div></div>
  <div class="card">
    <h2>N1–N10 制备批次记录</h2>
    <p class="hint">这里只登记<b>实际执行值</b>；未执行步骤不要按计划参数补填。分装数不会计入独立全流程重复。</p>
    <div class="grid g3">
      <div class="field"><label>样本</label><select id="pb_sample"></select></div>
      <div class="field"><label>制备批次编号</label><input id="pb_code" placeholder="如 PB-001"></div>
      <div class="field"><label>SOP 版本</label><input id="pb_sop" value="neutral_N_v1"></div>
      <div class="field"><label>独立制备次数</label><input id="pb_ind" type="number" min="1" value="1"></div>
      <div class="field"><label>甲基化后分装数</label><input id="pb_aliq" type="number" min="1" value="1"></div>
    </div>
    <div class="field"><label>N1–N10 实测记录 (JSON)</label><textarea id="pb_steps">${esc(JSON.stringify({
      N1: { status: 'completed', actual_values: { sample_mg: null, dmso_ml: null, temperature_c: null, duration_h: null }, reagent_lots: {}, notes: '' },
      N2: { status: 'not_recorded', actual_values: {} },
      N3: { status: 'not_recorded', actual_values: {} },
      N4: { status: 'not_recorded', actual_values: {} },
      N5: { status: 'not_recorded', actual_values: {} },
      N6: { status: 'not_recorded', actual_values: {} },
      N7: { status: 'not_recorded', actual_values: {} },
      N8: { status: 'not_recorded', actual_values: {} },
      N9: { status: 'not_recorded', actual_values: {} },
      N10: { status: 'not_recorded', actual_values: {} }
    }, null, 2))}</textarea></div>
    <div class="row"><button class="btn" id="pb_save">保存制备记录</button></div>
    <div id="pb_out"></div>
  </div>
  <div class="card">
    <h2>GC–EI 方法版本</h2>
    <div class="grid g3">
      <div class="field"><label>方法 ID</label><input id="m_id" placeholder="如 SP2380_v1"></div>
      <div class="field"><label>色谱柱</label><input id="m_col" placeholder="固定相、长度、内径、膜厚"></div>
      <div class="field"><label>升温程序</label><input id="m_temp" placeholder="如 170℃ 2 min; 3℃/min ..."></div>
      <div class="field"><label>载气</label><input id="m_gas" placeholder="He"></div>
      <div class="field"><label>EI 能量 (eV)</label><input id="m_ei" type="number" value="70"></div>
      <div class="field"><label>还原标签</label><select id="m_red"><option>NaBD4</option><option>NaBH4</option><option>unknown</option></select></div>
    </div>
    <div class="row"><button class="btn" id="m_save">保存方法版本</button></div>
    <div id="m_out"></div>
  </div>`;

  $('#s_save').onclick = async () => {
    try {
      const parse = v => { try { return v.trim() ? JSON.parse(v) : {}; } catch { throw new Error('JSON 格式错误'); } };
      const r = await api('/samples', { method: 'POST', body: {
        code: $('#s_code').value || 'UNNAMED', source: $('#s_source').value || null,
        batch: $('#s_batch').value || null, dry_mass_mg: $('#s_mass').value ? +$('#s_mass').value : null,
        composition: parse($('#s_comp').value), modifications: parse($('#s_mod').value) } });
      state.sampleId = r.sample_id; invalidateFrom(0); updateSampleGate();
      toast(`已建档 ${r.sample_id}`); loadSamples();
    } catch (e) { toast(e.message, true); }
  };
  $('#pb_save').onclick = async () => {
    try {
      const sid = $('#pb_sample').value;
      if (!sid) throw new Error('请先建立并选择样本');
      const steps = JSON.parse($('#pb_steps').value);
      const r = await api(`/samples/${sid}/preparations`, { method: 'POST', body: {
        batch_code: $('#pb_code').value || 'UNNAMED', sop_version: $('#pb_sop').value,
        independent_preparation_count: +$('#pb_ind').value,
        post_methylation_aliquots: +$('#pb_aliq').value, steps } });
      state.sampleId = sid; state.preparationBatchId = r.preparation_batch_id; invalidateFrom(0);
      $('#pb_out').innerHTML = `<div class="note">已保存 <code>${esc(r.preparation_batch_id)}</code>，运行分析时可绑定。</div>`;
      toast('制备记录已保存');
    } catch (e) { toast(e.message, true); }
  };
  $('#m_save').onclick = async () => {
    try {
      const id = $('#m_id').value;
      if (!id) throw new Error('方法 ID 必填');
      const r = await api('/methods', { method: 'POST', body: {
        method_id: id, column: $('#m_col').value || null,
        temperature_program: $('#m_temp').value || null,
        carrier_gas: $('#m_gas').value || null,
        ei_energy_ev: $('#m_ei').value ? +$('#m_ei').value : null,
        reduction_route: $('#m_red').value } });
      state.methodId = r.method_id; state.reductionLabel = $('#m_red').value;
      invalidateFrom(0); updateSampleGate();
      $('#m_out').innerHTML = `<div class="note">已保存方法 <code>${esc(r.method_id)}</code> v${r.version}，还原标签 <b>${esc(state.reductionLabel)}</b></div>`;
      toast('方法版本已保存');
    } catch (e) { toast(e.message, true); }
  };
  loadSamples(); updateSampleGate();
}

/** 第 1 步门禁提示：明确告知还缺什么 */
function updateSampleGate() {
  const g = $('#s_gate');
  if (!g) return;
  const need = [];
  if (!state.sampleId) need.push('保存样本（点「建档」）');
  if (!state.methodId) need.push('保存 GC 方法版本（含还原标签）');
  g.innerHTML = need.length
    ? `<div class="note warn">进入下一步前还需：${need.join('　/　')}</div>`
    : `<div class="note">样本 <code>${esc(String(state.sampleId).slice(0, 20))}</code> 与方法
       <code>${esc(state.methodId)}</code> 均已就绪，可进入第 2 步。</div>`;
}
async function loadSamples() {
  const r = await api('/samples');
  $('#s_list').innerHTML = r.items.length ? `<table><thead><tr><th>ID</th><th>编号</th><th>批次</th><th>分支</th><th>组成</th></tr></thead><tbody>
    ${r.items.map(s => `<tr><td class="mono">${esc(s.sample_id)}</td><td>${esc(s.code)}</td><td>${esc(s.batch || '—')}</td>
    <td>${badge(s.branch)}</td><td class="mono">${esc(JSON.stringify(s.composition))}</td></tr>`).join('')}</tbody></table>`
    : '<div class="empty">暂无样本</div>';
  if ($('#pb_sample')) $('#pb_sample').innerHTML = r.items.map(s =>
    `<option value="${esc(s.sample_id)}">${esc(s.code)} · ${esc(s.sample_id)}</option>`).join('');
}

/* ============================================================ ② 数据导入 */
async function viewImport() {
  const [fmt, samples, methods] = await Promise.all([
    api('/imports/formats'), api('/samples'), api('/methods')]);
  const MB = (fmt.max_upload_bytes / 1048576).toFixed(0);
  const kindName = k => ({
    ei_reference_table: 'EI 质谱汇总表', peak_table: '峰表 / 结构化输入',
    document: '文档提取', spectrum_image: '谱图图片提取'
  }[k] || k);
  $('#main').innerHTML = `
  <div class="card">
    <h2>② 导入文件</h2>
    <div id="u_gate"></div>
    <p class="hint">选择本机文件即可导入，<b>一个入口吃下所有格式</b>：xlsx/xlsm 走 EI 汇总表导入，
      csv/tsv/json 走峰表，<b>图片 / PDF / Word 会自动进入「⊕ 谱图提取」做人工校准</b>。
      文件按内容哈希去重保存，<b>不覆盖原文</b>；单个文件上限 ${MB} MB；<b>压缩包不自动展开</b>。</p>
    <div id="drop" class="dropzone">
      <div class="dz-icon">⇪</div>
      <div class="dz-main">把文件拖到这里，或 <button class="btn sec" id="pickBtn">选择文件</button></div>
      <div class="dz-sub">支持 ${fmt.supported_extensions.join(' · ')}</div>
      <input type="file" id="fileInput" hidden accept="${fmt.supported_extensions.join(',')}">
    </div>
    <div class="row" style="margin-top:10px">
      <div class="field" style="min-width:170px"><label>用途</label>
        <select id="u_kind"><option value="">自动识别</option>
          <option value="ei_reference_table">EI 质谱汇总表</option>
          <option value="peak_table">峰表 / 定性离子表</option></select></div>
      <div class="field" style="min-width:120px"><label>来源等级</label>
        <select id="u_tier"><option>E2</option><option>E1</option><option>E3</option><option>E4</option><option>unreviewed</option></select></div>
      <div class="field" style="min-width:150px"><label>绑定方法版本</label><input id="u_method" placeholder="可选，如 SP2380_v1"></div>
      <label class="chk"><input type="checkbox" id="u_commit"> 直接入库（不勾选则只预览）</label>
    </div>
    <div id="u_out"></div>
  </div>

  <div class="card">
    <h2>支持的文件格式</h2>
    <p class="hint">导入时即固化输入模式，后续评分器不得切换。</p>
    <table><thead><tr><th>扩展名</th><th>用途</th><th>说明</th></tr></thead><tbody>
      ${fmt.formats.map(f => `<tr><td class="mono">${esc(f.extension)}</td>
        <td>${esc(kindName(f.kind))}</td>
        <td>${esc(f.label)}<br><span style="color:var(--text-3)">${esc(f.notes)}</span></td></tr>`).join('')}
    </tbody></table>
    <div class="note warn" style="margin-top:10px">不接受：${fmt.rejected.archives.join(' · ')} —— ${esc(fmt.rejected.reason)}</div>
    <div style="font-size:12px;color:var(--text-3);margin-top:8px">${fmt.notes.map(n => `· ${esc(n)}`).join('<br>')}</div>
  </div>

  <div class="card">
    <h2>或用服务端路径导入</h2>
    <p class="hint">文件已在服务器/挂载盘上时可直接给路径。识别合并单元格、字体/填充颜色与说明文字；颜色语义<b>不换算为强度</b>。</p>
    <div class="grid g2">
      <div class="field"><label>服务端可见的 xlsx 路径</label><input id="i_path" placeholder="可选；一般直接使用上方浏览器上传"></div>
      <div class="field"><label>来源等级</label><select id="i_tier"><option>E2</option><option>E1</option><option>E3</option><option>E4</option><option>unreviewed</option></select></div>
    </div>
    <div class="row"><button class="btn sec" id="i_preview">① 预览解析</button><button class="btn" id="i_commit">② 提交入库</button></div>
    <div id="i_out"></div>
  </div>

  <div class="card">
    <h2>峰表 / 定性离子表</h2>
    <p class="hint">定性模式 ion 用 <code>marker</code>（<code>+</code> 或 <code>red</code>）；数值模式用 <code>intensity</code>。两种模式不可混用。</p>
    <div class="field"><label>峰表 JSON</label><textarea id="p_json">${esc(JSON.stringify({
      components: [{ component_id: 'peak_007', peak_label: 'P7', rt: 23.4, area: 12345,
        evidence_type: 'qualitative_ion_list',
        ions: [{ mz: 118, marker: 'red' }, { mz: 113, marker: '+' }, { mz: 99, marker: '+' }, { mz: 102, marker: '+' }, { mz: 233, marker: '+' }] }]
    }, null, 2))}</textarea></div>
    <div class="row">
      <div class="field" style="min-width:160px"><label>绑定样本</label><select id="p_sample"><option value="">未绑定</option>${samples.items.map(s => `<option value="${esc(s.sample_id)}">${esc(s.code)}</option>`).join('')}</select></div>
      <div class="field" style="min-width:160px"><label>绑定制备批次 ID</label><input id="p_prep" placeholder="可选" value="${esc(state.preparationBatchId || '')}"></div>
      <div class="field" style="min-width:160px"><label>绑定 GC 方法</label><select id="p_method"><option value="">未绑定</option>${methods.items.map(m => `<option value="${esc(m.method_id)}">${esc(m.method_id)}</option>`).join('')}</select></div>
      <div class="field" style="min-width:150px"><label>NaBD4/NaBH4 标签</label>
        <select id="p_label"><option value="NaBD4">NaBD4</option><option value="NaBH4">NaBH4</option><option value="unknown" selected>unknown</option></select></div>
      <div class="field" style="min-width:150px"><label>已检出单糖（逗号分隔）</label><input id="p_comp" placeholder="Glc,Gal"></div>
      <div class="field" style="min-width:130px"><label>观测 RI（可选）</label><input id="p_ri" type="number" step="0.1" placeholder="如 2050"></div>
      <div class="field" style="min-width:140px"><label>观测 RT/min（可选）</label><input id="p_rt" type="number" step="0.01" placeholder="如 23.4"></div>
      <div class="field" style="min-width:250px"><label title="只声明峰表覆盖完整采集范围；不会自动猜分子离子或硬删糖骨架"><input type="checkbox" id="p_full" style="width:auto;margin-right:6px">仪器导出的完整峰表</label></div>
      <div class="field" style="min-width:260px"><label title="仅当单糖组成方法、标准和检出能力均已验证，且组成列表可视为完整时勾选"><input type="checkbox" id="p_comp_complete" style="width:auto;margin-right:6px">单糖组成列表已验证完整</label></div>
      <button class="btn" id="p_run">解析为峰表 → 下一步</button>
    </div>
    <div class="note" style="font-size:12px">“完整峰表”只允许把缺失的参考强峰作为<b>软降权</b>，不会启动分子量硬门控。
      PMAA 的分子量随甲基/乙酰取代数变化，不能按 Hex/dHex/Pen 各设一个固定 MW。
      RI 定种为<b>正交通道</b>：无同方法标准时静默，不改变现有结论。</div>
    <div id="p_out"></div>
  </div>`;

  updateImportGate();

  /* ---------- 上传交互 ---------- */
  const input = $('#fileInput'), drop = $('#drop');
  $('#pickBtn').onclick = e => { e.stopPropagation(); input.click(); };
  drop.onclick = () => input.click();
  ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) doUpload(f); });
  input.onchange = () => { if (input.files[0]) doUpload(input.files[0]); };

  async function doUpload(file) {
    if (file.size > fmt.max_upload_bytes) { toast(`文件超过 ${MB} MB 上限`, true); return; }
    $('#u_out').innerHTML = `<div class="note">正在上传 ${esc(file.name)}（${(file.size / 1024).toFixed(0)} KB）…</div>`;
    const fd = new FormData();
    fd.append('file', file);
    if ($('#u_kind').value) fd.append('kind', $('#u_kind').value);
    if ($('#u_method').value) fd.append('method_id', $('#u_method').value);
    fd.append('evidence_tier', $('#u_tier').value);
    fd.append('commit', $('#u_commit').checked ? 'true' : 'false');
    try {
      const res = await fetch('/v1/imports/upload', { method: 'POST', body: fd });
      const d = await res.json();
      if (!res.ok) throw new Error((d.error_code ? `[${d.error_code}] ` : '') + (d.message || res.status));
      // 图片 / PDF / Word：服务端已自动转提取流程，交给第 3 步做人工校准
      if (d.needs_calibration) {
        state.extractInitial = { data: d, file };
        invalidateFrom(1);
        completeStep(1, { filename: file.name, sha256: d.file?.sha256,
          kind: d.provenance === 'derived_from_image' ? 'spectrum_image' : 'document' });
        $('#u_out').innerHTML = `<div class="note"><b>${esc(file.name)}</b> 已读取，识别为
          <b>${esc(d.provenance === 'derived_from_image' ? '质谱图' : '文档')}</b>，
          已自动进入第 3 步「提取峰值」做人工校准。</div>`;
        toast('已读取，进入第 3 步'); go('extract'); return;
      }
      renderUploadResult(d);
      toast(`已解析 ${file.name}`); refreshFooter();
    } catch (e) { $('#u_out').innerHTML = `<div class="note danger">${esc(e.message)}</div>`; toast(e.message, true); }
  }

  function renderUploadResult(d) {
    const f = d.file;
    let body = `<div class="note"><b>${esc(f.filename)}</b> · ${(f.byte_size / 1024).toFixed(0)} KB ·
      SHA-256 <code>${esc(f.sha256.slice(0, 24))}…</code> · ${f.reused ? '内容已存在，未重复保存' : '已保存'}</div>`;
    if (d.kind === 'ei_reference_table') {
      body += `<div class="note">共 ${d.summary.total_rows} 行 · 入库 ${d.summary.imported} · 分支隔离 ${d.summary.quarantined} · 错误隔离 ${d.summary.isolated}
        ${$('#u_commit').checked ? '' : '（预览未写库）'}</div>
        <div class="scroll"><table><thead><tr><th>源行</th><th>名称</th><th>状态</th><th>推导连接位</th><th>源表连接</th><th>离子</th></tr></thead><tbody>
        ${d.rows.map(x => `<tr><td class="mono">${esc(x.source_row_id)}</td><td>${esc(x.derivative_name_raw)}</td>
          <td>${badge(x.status)}</td><td class="mono">${esc(JSON.stringify(x.derived_linkage))}</td>
          <td class="mono">${esc(x.source_linkage || '—')}</td><td>${x.preview?.ion_count ?? '—'}</td></tr>`).join('')}
        </tbody></table></div>`;
    } else {
      window.__uploadedComponents = d.components;
      state.peakMeta = { sample_id: $('#p_sample').value || state.sampleId,
        preparation_batch_id: $('#p_prep').value || state.preparationBatchId,
        method_id: $('#p_method').value || state.methodId,
        sample_reduction_label: $('#p_label').value,
        composition_detected: $('#p_comp').value.split(',').map(s => s.trim()).filter(Boolean),
        composition_detection_limit_known: $('#p_comp_complete').checked };
      invalidateFrom(1);
      completeStep(1, { filename: f.filename, sha256: f.sha256, kind: 'peak_table',
                        components: d.components, count: d.count });
      updateImportGate();
      body += `<div class="note">解析出 ${d.count} 个组分。
        <button class="btn sec" id="u_run" style="margin-left:8px">进入第 3 步「提取峰值」→</button></div>
        <div class="scroll"><table><thead><tr><th>组分</th><th>RT</th><th>面积</th><th>模式</th><th>离子数</th></tr></thead><tbody>
        ${d.components.map(x => `<tr><td class="mono">${esc(x.component_id)}</td><td>${x.rt ?? '—'}</td>
          <td>${x.area ?? '—'}</td><td>${esc(x.evidence_type)}</td><td>${(x.ions || []).length}</td></tr>`).join('')}
        </tbody></table></div>`;
    }
    $('#u_out').innerHTML = body;
    const run = $('#u_run');
    if (run) run.onclick = () => {
      state.peakMeta = { sample_id: $('#p_sample').value || state.sampleId,
        preparation_batch_id: $('#p_prep').value || state.preparationBatchId,
        method_id: $('#p_method').value || state.methodId,
        sample_reduction_label: $('#p_label').value,
        composition_detected: $('#p_comp').value.split(',').map(s => s.trim()).filter(Boolean),
        composition_detection_limit_known: $('#p_comp_complete').checked };
      go('extract');
    };
  }

  const render = (r, committed) => {
    $('#i_out').innerHTML = `
      <div class="note">共 ${r.summary.total_rows} 行 · 入库 ${r.summary.imported} · 隔离分支 ${r.summary.quarantined} · 错误隔离 ${r.summary.isolated}
      ${committed ? '' : '（预览未写库）'}</div>
      <div class="grid g2">
        <div><h3>识别到的布局</h3><dl class="kv">
          <dt>工作表</dt><dd class="mono">${esc(r.summary.layout.sheet)}</dd>
          <dt>m/z 列数</dt><dd>${r.summary.layout.ion_columns}（${r.summary.layout.mz_range?.[0]}–${r.summary.layout.mz_range?.[1]}）</dd>
          <dt>数据行</dt><dd>${r.summary.layout.data_rows}</dd>
          <dt>分组行</dt><dd>${esc(JSON.stringify(r.summary.layout.section_rows))}</dd>
          <dt>合并区</dt><dd class="mono">${esc((r.summary.layout.merged_ranges || []).join(', '))}</dd>
        </dl></div>
        <div><h3>原表声明的语义</h3><dl class="kv">
          <dt>红色</dt><dd>${r.summary.detected_semantics.red_means === 'reported_gt20pct' ? '相对强度 &gt;20%（仅记语义，不换算强度）' : '未明确，已降级为 reported_present'}</dd>
          <dt>"+"</dt><dd>${esc(r.summary.detected_semantics.plus_means || '—')}</dd>
          <dt>还原试剂</dt><dd>${esc(r.summary.detected_semantics.reduction_label || 'unknown')}</dd>
        </dl></div>
      </div>
      <div class="scroll" style="margin-top:12px"><table><thead><tr>
        <th>源行</th><th>名称</th><th>状态</th><th>推导连接位</th><th>源表连接</th><th>骨架</th><th>离子</th><th>说明</th></tr></thead><tbody>
        ${r.rows.map(x => `<tr><td class="mono">${esc(x.source_row_id)}</td><td>${esc(x.derivative_name_raw)}</td>
          <td>${badge(x.status)}</td><td class="mono">${esc(JSON.stringify(x.derived_linkage))}</td>
          <td class="mono">${esc(x.source_linkage || '—')}</td><td>${esc(x.preview?.skeleton || '—')}</td>
          <td>${x.preview?.ion_count ?? '—'}${x.preview?.red_ion_count ? ` <span class="tag red">红${x.preview.red_ion_count}</span>` : ''}</td>
          <td style="max-width:280px;font-size:11.5px;color:var(--text-2)">${esc((x.messages || []).join('；') || '—')}</td></tr>`).join('')}
      </tbody></table></div>`;
  };

  $('#i_preview').onclick = async () => { try { render((await api('/imports/preview', { method: 'POST', body: { path: $('#i_path').value, evidence_tier: $('#i_tier').value } })), false); toast('预览完成'); } catch (e) { toast(e.message, true); } };
  $('#i_commit').onclick = async () => { try { render((await api('/imports/commit', { method: 'POST', body: { path: $('#i_path').value, evidence_tier: $('#i_tier').value, reviewer: 'web' } })), true); toast('已提交入库'); refreshFooter(); } catch (e) { toast(e.message, true); } };

  $('#p_run').onclick = () => {
    try {
      let comps;
      try { comps = JSON.parse($('#p_json').value).components || []; } catch { throw new Error('峰表 JSON 解析失败'); }
      if (!comps.length) throw new Error('峰表为空');
      // 观测 RI/RT：JSON 内逐峰值优先，表单统一默认值仅在组分未自带时填充
      const riIn = $('#p_ri').value ? +$('#p_ri').value : null;
      const rtIn = $('#p_rt').value ? +$('#p_rt').value : null;
      if (riIn != null || rtIn != null) comps.forEach(c => {
        if (riIn != null && c.retention_index == null) c.retention_index = riIn;
        if (rtIn != null && c.retention_time == null) c.retention_time = rtIn;
      });
      // 完整峰表只声明“缺失离子可被解释为软负证据”；不触发分子量/骨架硬门控。
      if ($('#p_full').checked) comps.forEach(c => {
        if (c.spectrum_completeness == null) c.spectrum_completeness = 'full_acquired_range';
      });
      state.peakMeta = { sample_id: $('#p_sample').value || state.sampleId,
        preparation_batch_id: $('#p_prep').value || state.preparationBatchId,
        method_id: $('#p_method').value || state.methodId,
        sample_reduction_label: $('#p_label').value,
        composition_detected: $('#p_comp').value.split(',').map(s => s.trim()).filter(Boolean),
        composition_detection_limit_known: $('#p_comp_complete').checked };
      invalidateFrom(1);
      completeStep(1, { filename: '手工录入峰表', kind: 'peak_table', components: comps, count: comps.length });
      toast(`已解析 ${comps.length} 个组分`); go('extract');
    } catch (e) { toast(e.message, true); }
  };
}

/** 第 2 步门禁提示：区分「参考库文件」与「样品数据文件」 */
function updateImportGate() {
  const g = $('#u_gate');
  if (!g) return;
  const f = WIZ.data.file;
  const name = { ei_reference_table: 'EI 参考库', peak_table: '峰表',
                 document: '文档', spectrum_image: '谱图图片' }[f?.kind] || f?.kind || '—';
  g.innerHTML = f
    ? `<div class="note">已导入 <b>${esc(f.filename)}</b>（${esc(name)}）
       ${f.count ? ` · ${f.count} 个组分` : ' · 待第 3 步提取峰值'}。可进入第 3 步。</div>`
    : `<div class="note warn">尚未导入样品数据。<b>参考库 xlsx 只更新参考库、不解锁下一步</b>——
       请再导入峰表 / 谱图 / 文档作为本次分析的输入。</div>`;
}

/* ============================================================ ⊕ 谱图提取
   从 PDF / Word / 图片中反解质谱峰。
   设计原则：机器负责"读成候选"，人负责"确认为可用"——
   因此这里所有峰都可编辑、可删除，且必须点确认才能进入分析。
------------------------------------------------------------------------ */
async function viewExtract() {
  // 第 2 步导入的是结构化峰表 → 走核对直通分支，无需图像反解与轴校准
  if (WIZ.data.file?.kind === 'peak_table') { await viewExtractFromPeakTable(WIZ.data.file); return; }
  const fmt = await api('/imports/formats');
  const [dmz0, dmz1] = fmt.default_mz_range || [40, 350];
  const imgExt = (fmt.groups?.image || []).join(',');
  const docExt = (fmt.groups?.document || []).join(',');

  $('#main').innerHTML = `
  <div class="card">
    <h2>从文档 / 图片提取质谱峰</h2>
    <p class="hint">支持 PDF（先抽文本表格，无文本才渲染页面）、Word（.doc/.docx）、以及 PNG/JPEG/TIFF/WebP/BMP 谱图。
      结果一律视为<b>推导值</b>，必须人工核对后才能使用。</p>
    ${fmt.ocr_available
      ? `<div class="note">已启用 OCR：可自动识别 x 轴刻度并<b>建议</b> m/z 轴范围（会给出拟合 R² 与识别到的刻度数）。
         建议值仍须你核对后再使用。</div>`
      : `<div class="note warn">未启用 OCR：安装可选依赖 <code>rapidocr-onnxruntime</code> 后可自动识别轴刻度；
         当前 m/z 轴范围需要你手动填写。</div>`}
    <div class="note warn">关于精度：图片反解的<b>m/z 与相对强度都只是候选值</b>。
      m/z 受轴范围、裁切和绘图区识别影响；强度受线宽、压缩、文字重叠、基线与峰簇影响。
      合成图测试不能替代真实仪器谱验证，请逐峰核对，尤其是用于区分候选的强峰。</div>
    ${(fmt.legacy_doc?.extensions || []).length ? `<div class="note">不支持 ${fmt.legacy_doc.extensions.join(' / ')}：${esc(fmt.legacy_doc.reason)}</div>` : ''}

    <div id="xdrop" class="dropzone">
      <div class="dz-icon">⊕</div>
      <div class="dz-main">拖入 PDF / Word / 谱图，或 <button class="btn sec" id="xpick">选择文件</button></div>
      <div class="dz-sub">${esc(docExt)} · ${esc(imgExt)}</div>
      <input type="file" id="xfile" hidden accept="${docExt},${imgExt}">
    </div>

    <div class="row" style="margin-top:10px">
      <div class="field" style="max-width:130px"><label>m/z 轴起点 *</label><input id="x_mz0" type="number" placeholder="如 ${dmz0}"></div>
      <div class="field" style="max-width:130px"><label>m/z 轴终点 *</label><input id="x_mz1" type="number" placeholder="如 ${dmz1}"></div>
      <div class="field" style="max-width:150px"><label>最小显著度</label><input id="x_prom" type="number" step="0.005" value="0.02"></div>
      <div class="field" style="max-width:120px"><label>最小峰距(px)</label><input id="x_dist" type="number" value="3"></div>
      <button class="btn sec" id="x_prev" disabled>← 上一张</button>
      <div class="field" style="min-width:230px;max-width:320px"><label>当前内嵌图片 <span id="x_img_pos">1 / 1</span></label><select id="x_img"><option value="1">图 1</option></select></div>
      <button class="btn sec" id="x_next" disabled>下一张 →</button>
      <button class="btn" id="x_run">重新提取</button>
    </div>
    <div class="note">图像处理的是<b>比例</b>：强度按基峰归一到 100；m/z 只有在你填写轴范围后才会映射。
      两个字段故意不预填，因为不同谱图范围可能不同；<b>轴范围填错会使全部峰整体错位</b>。</div>
    <div id="x_out"></div>
  </div>`;

  let LAST = null;   // 当前文件与当前图片结果
  const RESULTS = new Map(); // 每张图片的服务端提取结果，切换时不丢失
  const DRAFTS = new Map();  // 每张图片经人工编辑的峰表与独立轴范围
  const SAVED = new Map();   // 已明确保存、可批量确认的谱图
  const SKIPPED = new Set(); // 明确标记为非谱图/无需分析的图片
  let INVENTORY = [];

  const selectedIndex = () => Math.max(0, (+$('#x_img')?.value || 1) - 1);
  const imageLabel = i => {
    const item = INVENTORY.find(x => x.image_index === i);
    return item?.label || '';
  };
  const registerResult = d => {
    const idx = d.selected_image_index ?? 0;
    RESULTS.set(idx, d);
    if (Array.isArray(d.image_inventory) && d.image_inventory.length) INVENTORY = d.image_inventory;
    else if (!INVENTORY.length) INVENTORY = [{ image_index: 0, display_index: 1, label: null }];
  };
  const readCurrent = d => {
    const idx = d?.selected_image_index ?? 0;
    const peaks = [];
    for (const tr of document.querySelectorAll('#xpeaks tr')) {
      const mz = parseFloat(tr.querySelector('.xp_mz')?.value);
      const inten = parseFloat(tr.querySelector('.xp_int')?.value);
      if (isNaN(mz)) continue;
      peaks.push({ mz, intensity: isNaN(inten) ? 0 : inten,
        marker: tr.querySelector('.xp_mark')?.value || '+' });
    }
    const a = parseFloat($('#x_mz0')?.value), b = parseFloat($('#x_mz1')?.value);
    const label = imageLabel(idx);
    return {
      image_index: idx,
      component_id: `extracted_image_${String(idx + 1).padStart(3, '0')}`,
      label: label ? `${label} · 图 ${idx + 1}` : `${d?.file?.filename || '文档'} · 图 ${idx + 1}`,
      peaks,
      qualitative: !$('#x_numeric')?.checked,
      provenance: d?.provenance || 'derived_from_document',
      extraction_params: { ...(d?.params || {}), image_index: idx,
        mz_min: isNaN(a) ? null : a, mz_max: isNaN(b) ? null : b,
        source_label: label || null },
      axis_mz_min: isNaN(a) ? '' : a,
      axis_mz_max: isNaN(b) ? '' : b,
    };
  };
  const rememberDraft = () => {
    if (!LAST?.data || !document.querySelector('#xpeaks')) return;
    const idx = LAST.data.selected_image_index ?? 0;
    if (!SKIPPED.has(idx)) DRAFTS.set(idx, readCurrent(LAST.data));
  };
  const batchStatusHtml = () => {
    if (INVENTORY.length <= 1) return '';
    const rows = INVENTORY.map(item => {
      const i = item.image_index;
      const status = SAVED.has(i) ? `已保存（${SAVED.get(i).peaks.length} 峰）`
        : SKIPPED.has(i) ? '已忽略' : DRAFTS.has(i) ? '有未保存修改' : '待处理';
      const stateName = SAVED.has(i) ? 'accepted' : SKIPPED.has(i) ? 'rejected' : 'needs_review';
      return `<tr><td>${i + 1}</td><td>${esc(item.label || '—')}</td><td>${badge(stateName)}</td>
        <td>${esc(status)}</td></tr>`;
    }).join('');
    return `<div class="note"><b>批量进度：</b>已保存 ${SAVED.size} / ${INVENTORY.length}，已忽略 ${SKIPPED.size}。
      每张图分别校准；只有明确保存或忽略后才能整批进入审核。</div>
      <div class="scroll" style="max-height:190px"><table><thead><tr><th>图</th><th>文档标签</th><th>状态</th><th>详情</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  };
  const refreshBatchStatus = () => {
    const box = $('#x_batch_status');
    if (box) box.innerHTML = batchStatusHtml();
    const done = $('#x_finish_all');
    if (done) done.textContent = `确认全部已保存谱图（${SAVED.size}）→ 审核`;
  };

  const input = $('#xfile'), drop = $('#xdrop');
  $('#xpick').onclick = e => { e.stopPropagation(); input.click(); };
  drop.onclick = () => input.click();
  ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) run(f); });
  input.onchange = () => { if (input.files[0]) run(input.files[0]); };
  $('#x_run').onclick = () => { if (LAST?.file) run(LAST.file); else toast('请先选择文件', true); };

  // 若从「数据导入」页自动转来，直接渲染服务端已提取好的结果
  if (state.extractInitial) {
    const init = state.extractInitial;
    state.extractInitial = null;
    LAST = { file: init.file, data: init.data };
    registerResult(init.data);
    render(init.data);
    toast(`已载入 ${init.data.peak_count ?? (init.data.extracted_peaks || []).length ?? 0} 个峰，请校准 m/z 轴`);
  }

  async function run(file, requestedIndex = null, useCurrentAxis = true, capture = true) {
    const isNewFile = !!LAST?.file && file !== LAST.file;
    if (capture && !isNewFile) rememberDraft();
    if (isNewFile) {
      RESULTS.clear(); DRAFTS.clear(); SAVED.clear(); SKIPPED.clear(); INVENTORY = [];
      $('#x_img').innerHTML = '<option value="1">1</option>';
      $('#x_img').value = '1';
    }
    const idx = requestedIndex == null ? selectedIndex() : requestedIndex;
    LAST = { ...(LAST || {}), file };
    $('#x_out').innerHTML = `<div class="note">正在提取 ${esc(file.name)} …</div>`;
    const fd = new FormData();
    fd.append('file', file);
    const a = parseFloat($('#x_mz0').value), b = parseFloat($('#x_mz1').value);
    if (useCurrentAxis && !isNaN(a)) fd.append('mz_min', a);
    if (useCurrentAxis && !isNaN(b)) fd.append('mz_max', b);
    fd.append('min_prominence_ratio', $('#x_prom').value || '0.02');
    fd.append('min_peak_distance', $('#x_dist').value || '3');
    fd.append('image_index', String(idx));
    try {
      const res = await fetch('/v1/imports/extract', { method: 'POST', body: fd });
      const d = await res.json();
      if (!res.ok) throw new Error((d.error_code ? `[${d.error_code}] ` : '') + (d.message || res.status));
      LAST.data = d;
      registerResult(d);
      DRAFTS.delete(idx);
      SAVED.delete(idx);
      SKIPPED.delete(idx);
      render(d);
      toast(`提取到 ${d.peak_count ?? (d.extracted_peaks || []).length ?? 0} 个峰`);
    } catch (e) { $('#x_out').innerHTML = `<div class="note danger">${esc(e.message)}</div>`; toast(e.message, true); }
  }

  async function switchImage(target) {
    if (!LAST?.file || !INVENTORY.length) { toast('请先上传包含谱图的文档', true); return; }
    const bounded = Math.max(0, Math.min(INVENTORY.length - 1, target));
    const current = LAST.data?.selected_image_index ?? 0;
    if (bounded === current) return;
    rememberDraft();
    $('#x_img').value = bounded + 1;
    if (RESULTS.has(bounded)) {
      LAST.data = RESULTS.get(bounded);
      render(LAST.data);
      return;
    }
    // 新图片不沿用上一张图片的轴范围；优先让本图 OCR，失败后由用户独立填写。
    $('#x_mz0').value = '';
    $('#x_mz1').value = '';
    delete $('#x_mz0').dataset.userEdited;
    delete $('#x_mz1').dataset.userEdited;
    await run(LAST.file, bounded, false, false);
  }

  function render(d) {
    registerResult(d);
    const idx = d.selected_image_index ?? 0;
    const draft = DRAFTS.get(idx);
    const peaks = draft?.peaks || d.extracted_peaks || [];
    const cal = d.calibrated || !!draft;
    const img = d.preview_image;
    // 文档表格/文本路径给出的是 "intensity"（原文数字），图像路径是 "relative_intensity"
    const intenOf = p => (p.relative_intensity ?? p.intensity ?? 0);
    let html = `<div class="note ${cal ? '' : 'warn'}">
      <b>${esc(d.file.filename)}</b> · 来源 ${esc(d.provenance)} · 置信度 ${badge(d.confidence || 'low')}
      · ${esc(d.message || '')}
    </div>`;
    (d.warnings || []).forEach(w => { html += `<div class="note warn">${esc(w)}</div>`; });
    // OCR 自动轴标定结果：明确告知来源与质量，并把建议值填进输入框
    if (d.axis_calibration) {
      const ac = d.axis_calibration;
      if (ac.ok) {
        html += `<div class="note"><b>OCR 已自动标定 m/z 轴</b>：
          识别到 ${ac.n_points} 个刻度，线性拟合 R²=${esc(ac.r2)}，
          轴范围 <b>${esc(ac.mz_min)} – ${esc(ac.mz_max)}</b>。
          ${esc(ac.message || '')}
          <button class="btn sec" id="x_applyaxis" style="margin-left:8px">用这个范围重新提取</button>
          <br><span style="color:var(--text-3)">刻度读数：${ac.points.map(p => `${esc(p.text)}@x${esc(p.x)}`).join('，')}</span></div>`;
        const a0 = $('#x_mz0'), a1 = $('#x_mz1');
        if (a0 && !a0.dataset.userEdited) a0.value = ac.mz_min;
        if (a1 && !a1.dataset.userEdited) a1.value = ac.mz_max;
      } else {
        html += `<div class="note warn">OCR 未能自动标定轴：${esc(ac.message || '刻度不足或拟合质量不达标')}。请手动填写轴范围。</div>`;
      }
    }
    if (d.doc_kind) html += `<div class="note">文档解析路径：<b>${esc(d.doc_kind)}</b>（表格/文本优先，其次内嵌图片，最后整页渲染）</div>`;
    if (d.embedded_images) {
      html += `<div class="note">文档含 <b>${d.embedded_images}</b> 张内嵌图片；当前是第 <b>${idx + 1}</b> 张。
        切换图片不会覆盖已保存结果；各图的 m/z 轴范围相互独立。</div>`;
      const imgIndex = $('#x_img');
      if (imgIndex) {
        imgIndex.innerHTML = INVENTORY.map(item =>
          `<option value="${item.image_index + 1}">${item.image_index + 1}${item.label ? ` · ${esc(item.label)}` : ''}</option>`).join('');
        imgIndex.value = idx + 1;
        imgIndex.onchange = () => switchImage(selectedIndex());
      }
    }

    const prev = $('#x_prev'), next = $('#x_next'), pos = $('#x_img_pos');
    if (pos) pos.textContent = `${idx + 1} / ${INVENTORY.length || 1}`;
    if (prev) {
      prev.disabled = INVENTORY.length <= 1 || idx <= 0;
      prev.onclick = () => switchImage(idx - 1);
    }
    if (next) {
      next.disabled = INVENTORY.length <= 1 || idx >= INVENTORY.length - 1;
      next.onclick = () => switchImage(idx + 1);
    }

    html += `<div id="x_batch_status">${batchStatusHtml()}</div>`;

    html += `<div class="xgrid">
      <div>
        <h4 style="font-size:13px;margin:8px 0">原图预览（对着核 m/z 轴）</h4>
        ${img ? `<img class="xpreview" src="${img}" alt="预览">`
              : `<div class="empty" style="padding:24px">文档走的是表格/文本路径，无图片预览</div>`}
        ${d.image ? `<div class="kv" style="margin-top:8px">
          <dt>尺寸</dt><dd>${d.image.width}×${d.image.height}</dd>
          <dt>绘图区</dt><dd>x ${d.image.plot_left}–${d.image.plot_right}</dd>
          <dt>基线 y</dt><dd>${d.image.baseline_y}</dd></div>` : ''}
      </div>
      <div>
        <h4 style="font-size:13px;margin:8px 0">峰表（可编辑 / 可删除，确认后才生效）</h4>
        <div class="scroll" style="max-height:340px">
        <table><thead><tr><th>m/z</th><th>强度%</th><th>标记</th><th>像素x</th><th></th></tr></thead><tbody id="xpeaks">
        ${peaks.map((p, i) => `<tr data-i="${i}">
          <td><input class="xp_mz" value="${p.mz ?? ''}" style="width:80px" ${cal || p.mz != null ? '' : 'placeholder="需填"'}></td>
          <td><input class="xp_int" value="${intenOf(p)}" style="width:70px"></td>
          <td><select class="xp_mark" style="width:104px">
            <option value="+" ${intenOf(p) < 20 ? 'selected' : ''}>普通峰</option>
            <option value="red" ${intenOf(p) >= 20 ? 'selected' : ''}>强峰 &gt;20%</option>
          </select></td>
          <td class="mono">${p.x_pixel ?? '—'}</td>
          <td><button class="btn sec xdel" data-i="${i}" style="padding:2px 8px">删</button></td>
        </tr>`).join('') || '<tr><td colspan="5">未提取到峰</td></tr>'}
        </tbody></table></div>
        <div class="row" style="margin-top:10px">
          <button class="btn sec" id="x_add">+ 手工加一行</button>
          <label class="chk" title="图像反解的强度是近似值，默认按定性离子表处理（更保守）">
            <input type="checkbox" id="x_numeric"> 按数值谱比对（用反解强度跑加权余弦）</label>
          <button class="btn" id="x_confirm">${INVENTORY.length > 1 ? '保存当前谱图' : '确认峰表并比对数据库'}</button>
          ${INVENTORY.length > 1 ? `<button class="btn sec" id="x_skip">此图不是谱图，忽略</button>
            <button class="btn" id="x_finish_all">确认全部已保存谱图（${SAVED.size}）→ 审核</button>` : ''}
        </div>
        <div class="note" style="font-size:12px">文档表格/文本路径解出的数值是原文数字，默认按数值谱；
          图像反解的强度是图上量出来的近似值，默认按<b>定性离子表</b>处理——
          勾选后才会当数值谱用。</div>
      </div>
    </div>`;
    $('#x_out').innerHTML = html;

    // 用户手改过轴范围后，不再被 OCR 建议值覆盖
    ['#x_mz0', '#x_mz1'].forEach(sel => {
      const n = $(sel);
      if (n) n.oninput = () => {
        n.dataset.userEdited = '1';
        if (SAVED.delete(idx)) refreshBatchStatus();
      };
    });
    const applyAxis = $('#x_applyaxis');
    if (applyAxis) applyAxis.onclick = () => { if (LAST?.file) run(LAST.file); };

    // 文档表格/文本给出的强度是原文数字，可默认按数值峰表；
    // 图像反解的强度是图上量出的近似值 → 默认按定性（更保守）
    const hasRealIntensity = peaks.some(p => p.intensity != null);
    const numBox = $('#x_numeric');
    if (numBox) numBox.checked = draft ? !draft.qualitative : hasRealIntensity;

    // 每张图片保留自己独立的轴范围，切换图片时恢复，不向其他图片传播。
    if (draft) {
      $('#x_mz0').value = draft.axis_mz_min;
      $('#x_mz1').value = draft.axis_mz_max;
    } else if (d.axis_calibration?.ok) {
      $('#x_mz0').value = d.axis_calibration.mz_min;
      $('#x_mz1').value = d.axis_calibration.mz_max;
    } else {
      $('#x_mz0').value = d.params?.mz_min ?? '';
      $('#x_mz1').value = d.params?.mz_max ?? '';
    }

    document.querySelectorAll('.xdel').forEach(b => b.onclick = () => {
      const tr = b.closest('tr'); tr.remove();
      const i = +b.dataset.i; if (LAST.data?.extracted_peaks) LAST.data.extracted_peaks[i] = null;
      if (SAVED.delete(idx)) refreshBatchStatus();
    });
    $('#x_add').onclick = () => {
      const tb = $('#xpeaks');
      const row = document.createElement('tr');
      row.innerHTML = `<td><input class="xp_mz" style="width:80px" placeholder="m/z"></td>
        <td><input class="xp_int" style="width:70px" placeholder="强度"></td>
        <td><select class="xp_mark" style="width:104px"><option value="+">普通峰</option><option value="red">强峰 &gt;20%</option></select></td><td class="mono">—</td>
        <td><button class="btn sec xdel2" style="padding:2px 8px">删</button></td>`;
      tb.appendChild(row);
      row.querySelector('.xdel2').onclick = () => row.remove();
      if (SAVED.delete(idx)) refreshBatchStatus();
    };

    const markDirty = () => {
      if (SAVED.delete(idx)) refreshBatchStatus();
      SKIPPED.delete(idx);
    };
    document.querySelectorAll('#xpeaks input, #xpeaks select').forEach(n => {
      n.addEventListener('input', markDirty);
      n.addEventListener('change', markDirty);
    });
    if (numBox) numBox.onchange = markDirty;

    const validateCurrent = () => {
      const item = readCurrent(d);
      if (!item.peaks.length) throw new Error(`图 ${idx + 1} 没有有效峰`);
      if (item.peaks.some(p => !(p.mz >= 20 && p.mz <= 800)))
        throw new Error(`图 ${idx + 1} 存在超出 20–800 的 m/z`);
      return item;
    };
    const submitConfirmed = async spectra => {
      const body = spectra.length === 1 && INVENTORY.length <= 1 ? spectra[0] : { spectra };
      const r = await api('/imports/extract/confirm', { method: 'POST', body });
      invalidateFrom(2);
      completeStep(2, r.components);
      toast(`已确认 ${r.count} 张谱图并生成 ${r.count} 个独立组分，进入第 4 步审核`);
      go('peaks');
    };

    $('#x_confirm').onclick = async () => {
      try {
        const item = validateCurrent();
        if (INVENTORY.length <= 1) {
          await submitConfirmed([item]);
          return;
        }
        DRAFTS.set(idx, item);
        SAVED.set(idx, item);
        SKIPPED.delete(idx);
        refreshBatchStatus();
        toast(`图 ${idx + 1} 已保存（${item.peaks.length} 个峰）；可继续切换下一张`);
      } catch (e) { toast(e.message, true); }
    };

    const skip = $('#x_skip');
    if (skip) skip.onclick = () => {
      SAVED.delete(idx);
      DRAFTS.delete(idx);
      SKIPPED.add(idx);
      refreshBatchStatus();
      toast(`图 ${idx + 1} 已明确忽略`);
    };

    const finish = $('#x_finish_all');
    if (finish) finish.onclick = async () => {
      const pending = INVENTORY.filter(x => !SAVED.has(x.image_index) && !SKIPPED.has(x.image_index));
      if (pending.length) {
        toast(`还有 ${pending.length} 张图片未处理：${pending.slice(0, 8).map(x => x.image_index + 1).join('、')}${pending.length > 8 ? '…' : ''}`, true);
        return;
      }
      if (!SAVED.size) { toast('没有已保存的谱图，不能进入审核', true); return; }
      try {
        await submitConfirmed([...SAVED.values()].sort((a, b) => a.image_index - b.image_index));
      } catch (e) { toast(e.message, true); }
    };
  }
}

/* ============================================================ ③ 提取峰值（峰表直通分支）
   第 2 步导入的若是结构化峰表，就无需图像反解，这里只做核对与确认。
-------------------------------------------------------------------------- */
async function viewExtractFromPeakTable(f) {
  const comps = f.components || [];
  const ionText = c => (c.ions || []).map(i => i.mz ?? i).join(', ');
  $('#main').innerHTML = `
  <div class="card">
    <h2>③ 提取峰值</h2>
    <div class="note">第 2 步导入的是<b>结构化峰表</b>，无需图像反解与轴校准。
      请核对下列组分，确认后进入第 4 步逐峰审核。</div>
    <div class="scroll" style="margin-top:12px">
      <table><thead><tr><th>组分</th><th>标签</th><th>RT</th><th>面积</th><th>模式</th><th>离子 (m/z)</th></tr></thead><tbody>
        ${comps.map(c => `<tr><td class="mono">${esc(c.component_id)}</td>
          <td>${esc(c.peak_label || '—')}</td><td>${c.rt ?? '—'}</td><td>${c.area ?? '—'}</td>
          <td>${esc(c.evidence_type)}</td>
          <td class="mono" style="max-width:460px;font-size:11.5px">${esc(ionText(c))}</td></tr>`).join('')}
      </tbody></table>
    </div>
    <div class="row" style="margin-top:12px">
      <button class="btn" id="pt_ok">确认 ${comps.length} 个组分 → 第 4 步</button>
    </div>
  </div>`;
  $('#pt_ok').onclick = () => {
    invalidateFrom(2); completeStep(2, comps);
    toast(`已确认 ${comps.length} 个组分`); go('peaks');
  };
}

/* ============================================================ ④ 峰候选审核
   逐峰处置：排除的峰不进入下游比对。derived 来源（图像/文档反解）重点提示。
-------------------------------------------------------------------------- */
async function viewPeakReview() {
  const comps = WIZ.data.peaks || [];
  const ionText = c => (c.ions || []).map(i => i.mz ?? i).join(', ');
  $('#main').innerHTML = `
  <div class="card">
    <h2>④ 峰候选审核</h2>
    <p class="hint">逐峰决定去留。<b>排除</b>的峰不会提交比对；来源为
      <code>derived_from_image</code> / <code>derived_from_document</code> 的峰来自反解，建议重点核对。</p>
    <div class="row" style="margin-top:6px">
      <button class="btn sec" id="pr_all">全部接受</button>
      <button class="btn sec" id="pr_none">全部排除</button>
      <span class="hint" id="pr_stat"></span>
    </div>
    <div class="scroll" style="margin-top:12px">
      <table><thead><tr><th>处置</th><th>组分</th><th>来源</th><th>模式</th><th>离子数</th><th>离子 (m/z)</th><th>备注</th></tr></thead>
      <tbody id="pr_body">
      ${comps.map((c, i) => `<tr data-i="${i}">
        <td><select class="pr_dec"><option value="accept">接受</option><option value="reject">排除</option></select></td>
        <td class="mono">${esc(c.component_id)}</td>
        <td><span class="tag" style="${/derived/.test(c.provenance || '') ? 'color:var(--warn)' : ''}">${esc(c.provenance || 'observed')}</span></td>
        <td>${esc(c.evidence_type)}</td>
        <td>${(c.ions || []).length}</td>
        <td class="mono" style="max-width:400px;font-size:11.5px">${esc(ionText(c))}</td>
        <td><input class="pr_note" placeholder="可选" style="width:130px"></td>
      </tr>`).join('') || '<tr><td colspan="7">无峰，请回到第 3 步重新提取</td></tr>'}
      </tbody></table>
    </div>
    <div class="row" style="margin-top:12px">
      <button class="btn" id="pr_ok">确认审核结果 → 第 5 步比对</button>
    </div>
  </div>`;

  const stat = () => {
    const all = [...document.querySelectorAll('.pr_dec')];
    const n = all.filter(s => s.value === 'accept').length;
    $('#pr_stat').innerHTML = `接受 <b>${n}</b> / ${all.length}`;
  };
  document.querySelectorAll('.pr_dec').forEach(s => s.onchange = stat);
  $('#pr_all').onclick = () => { document.querySelectorAll('.pr_dec').forEach(s => s.value = 'accept'); stat(); };
  $('#pr_none').onclick = () => { document.querySelectorAll('.pr_dec').forEach(s => s.value = 'reject'); stat(); };
  stat();

  $('#pr_ok').onclick = () => {
    const kept = [];
    document.querySelectorAll('#pr_body tr').forEach(tr => {
      const i = +tr.dataset.i, sel = tr.querySelector('.pr_dec');
      if (!sel || sel.value !== 'accept') return;
      const c = { ...comps[i] };
      const note = tr.querySelector('.pr_note')?.value?.trim();
      if (note) c.review_note = note;
      kept.push(c);
    });
    if (!kept.length) { toast('至少要接受一个峰才能继续', true); return; }
    invalidateFrom(3); completeStep(3, kept);
    toast(`已接受 ${kept.length} / ${comps.length} 个峰`); go('match');
  };
}

/* ============================================================ ⑤ 谱图比对
   只对第 4 步接受的峰提交参考库检索；结果供第 6 步挑选候选结构。
-------------------------------------------------------------------------- */
async function viewMatch() {
  const peaks = WIZ.data.review || [];
  const meta = state.peakMeta || {};
  const hasDerived = peaks.some(c => String(c.provenance || '').startsWith('derived'));
  const allObservedFull = peaks.length > 0 && peaks.every(c =>
    c.provenance === 'observed' && c.spectrum_completeness === 'full_acquired_range');
  $('#main').innerHTML = `
  <div class="card">
    <h2>⑤ 谱图比对</h2>
    <div class="note">将对第 4 步接受的 <b>${peaks.length}</b> 个峰提交 EI 参考库检索。
      这里看的是<b>匹配过程</b>（分数、覆盖、证据与缺项），具体候选结构在下一步。</div>
    <div class="row" style="margin-top:10px">
      <div class="field" style="min-width:190px"><label>GC 方法 ID</label>
        <input id="mt_method" value="${esc(meta.method_id || state.methodId || '')}"></div>
      <div class="field" style="min-width:150px"><label>还原标签</label><select id="mt_label">
        ${['NaBD4', 'NaBH4', 'unknown'].map(v =>
          `<option ${v === (meta.sample_reduction_label || state.reductionLabel || 'NaBD4') ? 'selected' : ''}>${v}</option>`).join('')}
      </select></div>
      <div class="field" style="min-width:160px"><label>已检出单糖（逗号分隔）</label>
        <input id="mt_comp" value="${esc((meta.composition_detected || []).join(','))}" placeholder="Glc,Gal"></div>
      <div class="field" style="min-width:240px"><label title="只有检测方法和检出能力已验证、列表可视为完整时才勾选"><input type="checkbox" id="mt_comp_complete" style="width:auto;margin-right:6px" ${meta.composition_detection_limit_known ? 'checked' : ''}>单糖组成列表已验证完整</label></div>
      <div class="field" style="min-width:260px"><label title="仅适用于仪器直接导出的完整峰表；图片/文档推导不能升级"><input type="checkbox" id="mt_full" style="width:auto;margin-right:6px" ${allObservedFull ? 'checked' : ''} ${hasDerived ? 'disabled' : ''}>仪器导出的完整峰表</label></div>
      <button class="btn" id="mt_run">运行谱图比对</button>
    </div>
    <div class="note ${hasDerived ? 'warn' : ''}" style="font-size:12px">
      ${hasDerived ? '当前包含图片/文档推导数据：完整峰表选项已锁定，缺失离子不作负证据。' :
        '完整峰表只启用参考强峰缺失的软降权；不会自动猜分子离子，也不会按固定 MW 删除糖骨架。'}
      排序分数只用于检索，不是正确概率；近邻候选必须一起复核。</div>
    <div id="mt_out"></div>
  </div>`;

  $('#mt_run').onclick = async () => {
    try {
      $('#mt_out').innerHTML = '<div class="note">正在比对…</div>';
      const components = peaks.map(c => ({...c,
        spectrum_completeness: (!hasDerived && $('#mt_full').checked)
          ? 'full_acquired_range'
          : (String(c.provenance || '').startsWith('derived') ? 'selected_ions' : (c.spectrum_completeness || 'unknown'))
      }));
      const r = await api('/analyses', { method: 'POST', body: {
        sample_id: meta.sample_id || state.sampleId,
        preparation_batch_id: meta.preparation_batch_id || state.preparationBatchId,
        method_id: $('#mt_method').value || null,
        sample_reduction_label: $('#mt_label').value,
        composition_detected: $('#mt_comp').value.split(',').map(s => s.trim()).filter(Boolean),
        composition_detection_limit_known: $('#mt_comp_complete').checked,
        components } });
      state.analysisId = r.analysis_id;
      invalidateFrom(4); completeStep(4, r);
      renderMatchResult(r);
      toast(`比对完成 ${r.analysis_id}`);
    } catch (e) { $('#mt_out').innerHTML = `<div class="note danger">${esc(e.message)}</div>`; toast(e.message, true); }
  };

  if (WIZ.data.analysis) renderMatchResult(WIZ.data.analysis);
}

function renderMatchResult(r) {
  const items = r.components || [];
  $('#mt_out').innerHTML = items.map(c => {
    const res = c.result || {};
    const cds = res.candidate_derivatives || [];
    const leadGap = cds.length > 1 ? cds[0].score - cds[1].score : null;
    return `<div class="card" style="box-shadow:none;border-color:var(--border-strong);margin-top:12px">
      <div class="row" style="align-items:center">
        <h3 style="flex:0 0 auto">${esc(c.peak_label || c.component_id)}</h3>
        <span class="tag mono">${esc(c.component_id)}</span><span style="flex:1"></span>
        ${badge(res.derivative_identity_level)} ${badge(res.residue_mapping_level)} ${badge(res.quantification_level)}
      </div>
      <div class="note ${leadGap != null && leadGap <= 0.05 ? 'warn' : ''}" style="margin-top:8px">
        第一行只是<b>检索排序首位</b>，不是结构诊断。分数不是概率。
        ${leadGap == null ? '' : `前两名差值 ${leadGap.toFixed(3)}${leadGap <= 0.05 ? '，按并列候选处理。' : '。'}`}</div>
      <table style="margin-top:8px"><thead><tr>
        <th>排序</th><th>检索分数（非概率）</th><th>候选化学实体</th><th>O-Me</th><th>等级</th></tr></thead><tbody>
      ${cds.slice(0, 8).map((d, idx) => `<tr>
        <td class="mono">#${idx + 1}${idx < 2 && leadGap != null && leadGap <= 0.05 ? ' · 并列带' : ''}</td>
        <td><div style="display:flex;align-items:center;gap:6px"><span class="mono">${d.score.toFixed(3)}</span>
          <span class="bar" style="width:60px"><i style="width:${(d.score * 100).toFixed(0)}%"></i></span></div>
          ${d.coverage != null ? `<div style="font-size:11px;color:var(--text-2);margin-top:2px">参考覆盖 ${d.coverage.toFixed(2)}${d.red_total ? ` · 参考强峰 ${d.red_matched}/${d.red_total}` : ''}${d.score_components?.missing_strong_penalty_applied ? ' · 已软降权' : ' · 未用缺失负证据'}</div>` : ''}</td>
        <td>${esc(d.derivative_name)}${d.low_coverage_warning ? ' <span class="tag warn" title="参考条目本身收录离子较少，排序对输入与库完整度敏感">⚠参考稀疏</span>' : ''}</td>
        <td class="mono">${esc((d.o_methyl_positions || []).join(',') || '—')}</td>
        <td>${badge(d.identity_level)}</td></tr>`).join('') || '<tr><td colspan="5">无候选</td></tr>'}
      </tbody></table>
      <div class="grid g2" style="margin-top:10px">
        <div><h4 style="font-size:13px;margin:0 0 4px">缺失证据</h4>
          <div>${(res.missing_evidence || []).map(m => `<span class="tag">${esc(m)}</span>`).join('') || '—'}</div></div>
        <div><h4 style="font-size:13px;margin:0 0 4px">冲突</h4>
          <div>${(res.conflicts || []).map(x => `<div class="note danger"><code>${esc(x.code)}</code> ${esc(x.message)}</div>`).join('')
            || '<div class="note">无冲突</div>'}</div></div>
      </div>
      <div style="font-size:12px;color:var(--text-2);margin-top:8px">
        ${(res.notes || []).map(n => `· ${esc(n)}`).join('<br>') || '—'}</div>
    </div>`;
  }).join('') + (items.length ? `<div class="note" style="margin-top:12px">
    比对完成，共 ${items.length} 个组分。可进入第 6 步查看候选结构。</div>` : '');
}

/* ============================================================ ⑥ 候选结构 */
function interpHTML(res) {
  const it = res.interpretation;
  if (!it) return '';
  const seg = (s, idx) => `<div class="card" style="box-shadow:none;border-color:var(--border);margin-top:8px;padding:10px 12px">
    <div class="row" style="align-items:center;gap:8px">
      <b style="font-size:13px">[${idx}] ${esc(s.tier)}</b>
      ${s.gated ? '<span class="tag">证据门控</span>' : ''}
    </div>
    <div style="font-size:13px;margin-top:4px;color:var(--text)">${esc(s.text)}</div>
  </div>`;
  return `<div style="margin-top:12px">
    <h4 style="font-size:13px;margin:0 0 4px">三段式判读（整体置信度：<b>${esc(it.confidence_tier)}</b>）</h4>
    ${seg(it.pmaa_identity, 1)}${seg(it.residue_linkage, 2)}${seg(it.sequence, 3)}
  </div>`;
}

async function viewCandidates() {
  const analysisId = WIZ.data.analysis?.analysis_id || state.analysisId;
  $('#main').innerHTML = `<div class="card"><h2>⑥ 候选结构</h2><p class="hint" id="rv_head">加载中…</p><div id="rv_body"></div></div>`;
  if (!analysisId) { $('#rv_head').textContent = '尚无分析任务，请先在第 5 步运行谱图比对。'; return; }
  const r = await api(`/analyses/${analysisId}/components`);
  $('#rv_head').innerHTML = `分析 <code>${esc(state.analysisId)}</code> · ${r.items.length} 个组分 ·
    算法证据在左，人工决定在右；<b>确认必须引用证据</b>，不能以勾选代替证据门槛。`;

  $('#rv_body').innerHTML = r.items.map(c => {
    const res = c.result || {};
    const cds = res.candidate_derivatives || [];
    const leadGap = cds.length > 1 ? cds[0].score - cds[1].score : null;
    return `<div class="card" style="box-shadow:none;border-color:var(--border-strong)">
      <div class="row" style="align-items:center">
        <h3 style="flex:0 0 auto">${esc(c.peak_label || c.component_id)}</h3>
        <span class="tag mono">${esc(c.component_id)}</span>
        <span class="tag">RT ${c.rt ?? '—'}</span>
        <span class="tag">${esc(c.evidence_type)}</span>
        <span style="flex:1"></span>
        ${badge(res.derivative_identity_level)} ${badge(res.residue_mapping_level)} ${badge(res.quantification_level)}
      </div>
      ${interpHTML(res)}
      <div class="grid g2" style="margin-top:10px">
        <div>
          <h4 style="font-size:13px;margin:6px 0">PMAA 检索候选（首位不是诊断）</h4>
          <div class="note ${leadGap != null && leadGap <= 0.05 ? 'warn' : ''}">分数只表示与当前参考库的相似排序，不是正确概率。
            ${leadGap == null ? '' : `前两名差 ${leadGap.toFixed(3)}${leadGap <= 0.05 ? '，必须作为并列候选复核。' : '。'}`}</div>
          <table><thead><tr><th>排序</th><th>检索分数</th><th>化学实体</th><th>O-Me</th><th>O-Ac</th><th>等级</th></tr></thead><tbody>
          ${cds.map((d, idx) => `<tr>
            <td class="mono">#${idx + 1}${idx < 2 && leadGap != null && leadGap <= 0.05 ? ' · 并列带' : ''}</td>
            <td><div style="display:flex;align-items:center;gap:6px"><span class="mono">${d.score.toFixed(3)}</span>
              <span class="bar" style="width:46px"><i style="width:${(d.score * 100).toFixed(0)}%"></i></span></div>
              ${d.coverage != null ? `<div style="font-size:11px;color:var(--text-2);margin-top:2px">参考覆盖 ${d.coverage.toFixed(2)}${d.red_total ? ` · 强峰 ${d.red_matched}/${d.red_total}` : ''}${d.score_components?.missing_strong_penalty_applied ? ' · 已软降权' : ' · 未用缺失负证据'}</div>` : ''}</td>
            <td>${esc(d.derivative_name)}${d.low_coverage_warning ? ' <span class="tag warn" title="参考条目本身收录离子较少，排名对输入与库完整度敏感">⚠参考稀疏</span>' : ''}</td>
            <td class="mono">${esc((d.o_methyl_positions || []).join(',') || '—')}</td>
            <td class="mono">${esc((d.o_acetyl_positions || []).join(','))}</td>
            <td>${badge(d.identity_level)}</td></tr>`).join('') || '<tr><td colspan="6">无候选</td></tr>'}
          </tbody></table>
          <h4 style="font-size:13px;margin:12px 0 6px">原残基候选（同一 PMAA 的多来源全部保留）</h4>
          <div>${(res.candidate_residues || []).map(x => `<span class="tag">${esc(x.residue_label)} <span style="opacity:.6">${esc(x.framework_id)}</span></span>`).join('') || '—'}</div>
          ${(res.ambiguities || []).length ? `<div class="note warn"><b>不可辨识关系</b>：${(res.ambiguities || []).map(a => `<code>${esc(a.ambiguity_id)}</code>（需 ${esc(a.required_evidence_code)}）`).join('、')}。换柱或更高分数都不能恢复该信息。</div>` : ''}
        </div>
        <div>
          <h4 style="font-size:13px;margin:6px 0">冲突</h4>
          <div>${(res.conflicts || []).map(x => `<div class="note danger"><code>${esc(x.code)}</code> ${esc(x.message)}</div>`).join('') || '<div class="note">无冲突</div>'}</div>
          <h4 style="font-size:13px;margin:12px 0 6px">缺失证据</h4>
          <div>${(res.missing_evidence || []).map(m => `<span class="tag">${esc(m)}</span>`).join('') || '—'}</div>
          <h4 style="font-size:13px;margin:12px 0 6px">建议补测</h4>
          <div>${(res.next_discriminating_evidence || []).map(m => `<span class="tag">${esc(m)}</span>`).join('') || '—'}</div>
          <h4 style="font-size:13px;margin:12px 0 6px">规则说明</h4>
          <div style="font-size:12px;color:var(--text-2)">${(res.notes || []).map(n => `· ${esc(n)}`).join('<br>') || '—'}</div>
          <div class="row" style="margin-top:14px">
            <div class="field"><label>决定 (JSON)</label><input class="rv_dec" data-cid="${esc(c.component_id)}" value='{"derivative_identity":"probable","evidence_ids":["F001"]}'></div>
          </div>
          <div class="row">
            <div class="field"><label>审核人</label><input class="rv_who" data-cid="${esc(c.component_id)}" value="reviewer"></div>
            <div class="field"><label>原因（必填）</label><input class="rv_why" data-cid="${esc(c.component_id)}" placeholder="依据说明"></div>
            <button class="btn sec rv_submit" data-cid="${esc(c.component_id)}">提交复核</button>
          </div>
        </div>
      </div></div>`;
  }).join('');

  document.querySelectorAll('.rv_submit').forEach(b => b.onclick = async () => {
    const cid = b.dataset.cid;
    try {
      const decision = JSON.parse($(`.rv_dec[data-cid="${cid}"]`).value);
      await api(`/analyses/${analysisId}/reviews`, { method: 'POST', body: {
        component_id: cid, decision, reviewer: $(`.rv_who[data-cid="${cid}"]`).value,
        reason: $(`.rv_why[data-cid="${cid}"]`).value } });
      toast('复核已记录（覆盖判断保留前值）');
    } catch (e) { toast(e.message, true); }
  });

  // 第 6 步完成动作：确认候选集 → 第 7 步
  const confirmed = (WIZ.data.candidates || []).map(c => c.component_id);
  $('#rv_body').insertAdjacentHTML('beforeend', `
    <div class="row" style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <button class="btn" id="cv_ok">确认候选集 → 第 7 步 AI 评议</button>
      <span class="hint">${confirmed.length ? `已确认 ${confirmed.length} 个组分的候选` : '查看候选结构后点此进入 AI 评议'}</span>
    </div>`);
  $('#cv_ok').onclick = () => {
    invalidateFrom(5);
    completeStep(5, r.items.map(c => ({
      component_id: c.component_id,
      candidates: (c.result?.candidate_derivatives || []).map(d => d.derivative_id) })));
    toast('候选集已确认'); go('ai');
  };
}

/* ============================================================ ⑦ AI 评议
   第 6 步已给出全部候选结构；这里让 AI 给出"最相关项 + 理由 + 置信度"，
   由人拍板（q-2：推荐 + 理由，人拍板）。接受建议不会自动提高证据等级。
-------------------------------------------------------------------------- */
async function viewAI() {
  const provs = await api('/llm/providers');
  const list = await api('/analyses');
  const aid = WIZ.data.analysis?.analysis_id || state.analysisId || (list.items[0] ? list.items[0].analysis_id : null);
  const firstCid = (WIZ.data.candidates || [])[0]?.component_id || 'peak_007';
  $('#main').innerHTML = `
  <div class="card">
    <h2>⑦ AI 评议</h2>
    <p class="hint">AI 基于前几步冻结的证据包，给出<b>最相关项 + 理由 + 置信度</b>；输出只是建议，
      <b>由你拍板</b>。接受不会自动提高证据等级——证据门槛仍在第 6 步的人工复核里。</p>
    <div class="row">
      <div class="field" style="min-width:200px"><label>分析任务</label><select id="l_an">
        ${list.items.map(a => `<option value="${esc(a.analysis_id)}" ${a.analysis_id === aid ? 'selected' : ''}>${esc(a.analysis_id)} (rev${a.revision})</option>`).join('')}
      </select></div>
      <div class="field" style="min-width:180px"><label>服务商配置</label><select id="l_prov">
        ${provs.items.map(p => `<option value="${esc(p.provider_id)}">${esc(p.name)} · ${esc(p.adapter)}</option>`).join('') || '<option value="">（未配置）</option>'}
      </select></div>
      <div class="field" style="min-width:130px"><label>组分 ID</label><input id="l_cid" value="${esc(firstCid)}"></div>
      <button class="btn" id="l_run">请求评议</button>
      <button class="btn sec" id="l_list">查看已有评议</button>
    </div>
    <div id="l_out"></div>
  </div>`;

  $('#l_run').onclick = async () => {
    try {
      const a = $('#l_an').value; state.analysisId = a;
      const r = await api(`/analyses/${a}/llm-assessments`, { method: 'POST', body: {
        analysis_revision: 1, component_ids: [$('#l_cid').value], provider_config_id: $('#l_prov').value } });
      renderAssessments(r.assessments);
    } catch (e) { toast(e.message, true); }
  };
  $('#l_list').onclick = async () => {
    try { const r = await api(`/analyses/${$('#l_an').value}/llm-assessments`); renderAssessments(r.items.map(i => ({ assessment_id: i.assessment_id, ...i }))); }
    catch (e) { toast(e.message, true); }
  };

  function renderAssessments(items) {
    $('#l_out').innerHTML = items.map(a => {
      const v = a.validation || {};
      const pr = a.parsed_response || {};
      return `<div class="card" style="box-shadow:none;border-color:var(--border-strong);margin-top:12px">
        <div class="row" style="align-items:center">
          <code>${esc(a.assessment_id || '')}</code>
          <span style="flex:1"></span>${badge(a.status)} ${badge(a.finish_status)}
          ${pr.confidence ? `<span class="tag ${pr.confidence === 'high' ? 'ok' : pr.confidence === 'low' ? 'warn' : ''}">置信度 ${esc(pr.confidence)}</span>` : ''}
          ${a.cached_reused ? '<span class="tag">缓存复用</span>' : ''}
        </div>
        ${a.error ? `<div class="note danger">${esc(a.error)} —— 已回退，规则结果未丢失。</div>` : ''}
        ${v.hard_failures?.length ? `<div class="note danger"><b>拒收</b>：${v.hard_failures.map(x => `<br>· <code>${esc(x.code)}</code> ${esc(x.message)}（${esc(x.rule)}）`).join('')}</div>` : ''}
        ${v.semantic_flags?.length ? `<div class="note warn"><b>需人工语义复核</b>：${v.semantic_flags.map(x => `<br>· <code>${esc(x.code)}</code> ${esc(x.message)}`).join('')}</div>` : ''}
        ${pr.summary ? `<h4 style="font-size:13px;margin:10px 0 4px">最相关项与理由</h4>
          <div class="note">${esc(pr.summary)}</div>
          <div>${(pr.candidate_reviews || []).map(r => `<span class="tag">${esc(r.candidate_id)}: ${esc(r.assessment)}</span>`).join('')}</div>
          <div style="margin-top:6px">保留歧义：${(pr.retained_ambiguity_ids || []).map(x => `<code>${esc(x)}</code>`).join('、') || '—'}</div>
          <div>建议处置：<b>${esc(pr.recommended_disposition)}</b></div>` : ''}
        ${a.assessment_id ? `<div class="row" style="margin-top:10px">
          <select id="d_${esc(a.assessment_id)}"><option value="accepted">接受（采纳建议）</option><option value="partially_accepted">部分接受</option><option value="rejected">拒绝</option></select>
          <input id="r_${esc(a.assessment_id)}" placeholder="审核人">
          <input id="w_${esc(a.assessment_id)}" placeholder="拍板理由（必填）">
          <button class="btn sec" data-aid="${esc(a.assessment_id)}">拍板 · 提交审核</button></div>
          <div class="note" style="font-size:12px">提交后本步标记完成，可点底部「完成流程」进入 QC 与报告。</div>` : ''}
      </div>`;
    }).join('') || '<div class="empty">暂无评议，点「请求评议」</div>';

    document.querySelectorAll('[data-aid]').forEach(b => b.onclick = async () => {
      const id = b.dataset.aid;
      const dec = $(`#d_${id}`).value;
      try {
        const r = await api(`/llm-assessments/${id}/reviews`, { method: 'POST', body: {
          decision: dec, reviewer: $(`#r_${id}`).value || 'reviewer', reason: $(`#w_${id}`).value } });
        if (dec === 'accepted' || dec === 'partially_accepted') {
          completeStep(6, { assessment_id: id, decision: dec });
          toast('已拍板，第 7 步完成'); renderWizBar();
        } else { toast(r.note || '已记录拒绝'); }
      } catch (e) { toast(e.message, true); }
    });
  }
}

/* ============================================================ ⑧ 完成：QC 与报告
   向导终点（q-3：QC 与报告吸收进流程末尾）。分析任务来自第 5 步。
-------------------------------------------------------------------------- */
async function viewFinish() {
  const aid = WIZ.data.analysis?.analysis_id || state.analysisId;
  if (!aid) { $('#main').innerHTML = `<div class="card"><h2>⑧ 完成：QC 与报告</h2><p class="hint">尚无分析任务，请先走完前 7 步。</p></div>`; return; }
  $('#main').innerHTML = `
  <div class="card">
    <h2>⑧ 完成：样本级 QC 与报告</h2>
    <p class="hint">分析任务 <code>${esc(aid)}</code>。QC 失败会<b>冻结</b>正式结构与定量放行，但候选全部保留供研究；
      报告可随时重导，重跑生成新版本不改写历史。</p>
    <div class="grid g3" style="margin-top:8px">
      <div class="field"><label>空白占比 (0–1)</label><input id="f_blank" type="number" step="0.01" value="0.02"></div>
      <div class="field"><label>独立制备次数</label><input id="f_prep" type="number" value="3"></div>
      <div class="field"><label>甲基化后分装数</label><input id="f_aliq" type="number" value="4"></div>
      <div class="field"><label>重复性 CV</label><input id="f_cv" type="number" step="0.01" value="0.10"></div>
      <div class="field"><label>回收率</label><input id="f_rec" type="number" step="0.01" value="0.95"></div>
      <div class="field"><label>未识别面积占比</label><input id="f_unid" type="number" step="0.01" value="0.08"></div>
    </div>
    <div class="row">
      <button class="btn" id="f_qc">计算 QC</button>
      <button class="btn sec" id="f_json">导出 JSON 报告</button>
      <button class="btn sec" id="f_html">导出 HTML 报告</button>
    </div>
    <div id="f_out"></div>
  </div>`;
  $('#f_qc').onclick = async () => {
    try {
      const r = await api(`/analyses/${aid}/qc`, { method: 'POST', body: {
        blank_peak_ratio: +$('#f_blank').value, independent_preparations: +$('#f_prep').value,
        post_methylation_aliquots: +$('#f_aliq').value, repeat_cv: +$('#f_cv').value,
        recovery: +$('#f_rec').value, unidentified_area_ratio: +$('#f_unid').value } });
      $('#f_out').innerHTML = `<div class="note ${r.qc_status === 'failed' ? 'danger' : ''}">
        状态 ${badge(r.qc_status)} · 正式报告 ${r.formal_report_allowed ? '允许' : '<b>已冻结</b>'}</div>
        <table><thead><tr><th>检查项</th><th>状态</th><th>说明</th></tr></thead><tbody>
        ${r.checks.map(c => `<tr><td class="mono">${esc(c.code)}</td><td>${badge(c.status)}</td><td>${esc(c.message)}</td></tr>`).join('')}
        </tbody></table>`;
    } catch (e) { toast(e.message, true); }
  };
  $('#f_json').onclick = async () => {
    try {
      const r = await api(`/analyses/${aid}/reports`, { method: 'POST', body: { fmt: 'json' } });
      $('#f_out').insertAdjacentHTML('beforeend', `<div class="note">类型 <b>${esc(r.kind)}</b> · 哈希 <code>${esc(r.sha256)}</code></div>
        <h4 style="font-size:13px;margin:8px 0 4px">局限与未决项</h4><ul style="font-size:12.5px">${r.limitations.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`);
    } catch (e) { toast(e.message, true); }
  };
  $('#f_html').onclick = async () => {
    try {
      const res = await fetch(`/v1/analyses/${aid}/reports`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fmt: 'html' }) });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.message || d.detail || `HTTP ${res.status}`); }
      const url = URL.createObjectURL(await res.blob()); window.open(url, '_blank', 'noopener'); setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) { toast(e.message, true); }
  };
}

/* ============================================================ ⑤ 样本级 QC */
async function viewQC() {
  const list = await api('/analyses');
  if (!state.analysisId && list.items[0]) state.analysisId = list.items[0].analysis_id;
  $('#main').innerHTML = `
  <div class="card">
    <h2>样本级 QC 与正式报告门</h2>
    <p class="hint">QC 失败将<b>冻结</b>正式结构与定量放行，但候选全部保留供研究，只能导出带失败标识的研究草稿。</p>
    <div class="row"><div class="field" style="min-width:220px"><label>分析任务</label><select id="q_an">
      ${list.items.map(a => `<option value="${esc(a.analysis_id)}" ${a.analysis_id === state.analysisId ? 'selected' : ''}>${esc(a.analysis_id)}</option>`).join('')}
    </select></div></div>
    <div class="grid g3" style="margin-top:8px">
      <div class="field"><label>空白占比 (0–1)</label><input id="q_blank" type="number" step="0.01" value="0.02"></div>
      <div class="field"><label>独立制备次数</label><input id="q_prep" type="number" value="3"></div>
      <div class="field"><label>甲基化后分装数</label><input id="q_aliq" type="number" value="4"></div>
      <div class="field"><label>重复性 CV</label><input id="q_cv" type="number" step="0.01" value="0.10"></div>
      <div class="field"><label>回收率</label><input id="q_rec" type="number" step="0.01" value="0.95"></div>
      <div class="field"><label>未识别面积占比</label><input id="q_unid" type="number" step="0.01" value="0.08"></div>
    </div>
    <div class="row"><button class="btn" id="q_run">计算 QC</button></div>
    <div id="q_out"></div>
  </div>`;
  $('#q_run').onclick = async () => {
    try {
      const r = await api(`/analyses/${$('#q_an').value}/qc`, { method: 'POST', body: {
        blank_peak_ratio: +$('#q_blank').value, independent_preparations: +$('#q_prep').value,
        post_methylation_aliquots: +$('#q_aliq').value, repeat_cv: +$('#q_cv').value,
        recovery: +$('#q_rec').value, unidentified_area_ratio: +$('#q_unid').value } });
      $('#q_out').innerHTML = `<div class="note ${r.qc_status === 'failed' ? 'danger' : ''}">
        状态 ${badge(r.qc_status)} · 正式报告 ${r.formal_report_allowed ? '允许' : '<b>已冻结</b>'}</div>
        <table><thead><tr><th>检查项</th><th>状态</th><th>说明</th></tr></thead><tbody>
        ${r.checks.map(c => `<tr><td class="mono">${esc(c.code)}</td><td>${badge(c.status)}</td><td>${esc(c.message)}</td></tr>`).join('')}
        </tbody></table>`;
    } catch (e) { toast(e.message, true); }
  };
}

/* ============================================================ ⑥ 报告 */
async function viewReport() {
  const list = await api('/analyses');
  if (!state.analysisId && list.items[0]) state.analysisId = list.items[0].analysis_id;
  $('#main').innerHTML = `
  <div class="card">
    <h2>报告导出与版本</h2>
    <p class="hint">重跑生成新版本，不改写历史报告；报告哈希随内容冻结。</p>
    <div class="row">
      <div class="field" style="min-width:220px"><label>分析任务</label><select id="rp_an">
        ${list.items.map(a => `<option value="${esc(a.analysis_id)}" ${a.analysis_id === state.analysisId ? 'selected' : ''}>${esc(a.analysis_id)}</option>`).join('')}
      </select></div>
      <button class="btn" id="rp_json">导出 JSON</button>
      <button class="btn sec" id="rp_html">导出 HTML</button>
    </div>
    <div id="rp_out"></div>
  </div>`;
  $('#rp_json').onclick = async () => {
    try { const r = await api(`/analyses/${$('#rp_an').value}/reports`, { method: 'POST', body: { fmt: 'json' } });
      $('#rp_out').innerHTML = `<div class="note">类型 <b>${esc(r.kind)}</b> · 哈希 <code>${esc(r.sha256)}</code></div>
        <h4 style="font-size:13px;margin:8px 0 4px">局限与未决项</h4><ul style="font-size:12.5px">${r.limitations.map(x => `<li>${esc(x)}</li>`).join('')}</ul>
        <pre class="json">${esc(JSON.stringify(r, null, 1))}</pre>`;
    } catch (e) { toast(e.message, true); }
  };
  $('#rp_html').onclick = async () => {
    try {
      const res = await fetch(`/v1/analyses/${$('#rp_an').value}/reports`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fmt: 'html' }) });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.message || d.detail || `HTTP ${res.status}`);
      }
      const url = URL.createObjectURL(await res.blob());
      window.open(url, '_blank', 'noopener');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) { toast(e.message, true); }
  };
}

/* ============================================================ 参考库 */
async function viewLibrary() {
  const r = await api('/references');
  const d = await api('/derivatives');
  $('#main').innerHTML = `
  <div class="card">
    <h2>参考库</h2><p class="hint">共 ${r.count} 条参考条目、${d.count} 个化学实体。同一实体若有多来源，必须全部保留。</p>
    <div class="scroll"><table><thead><tr><th>源行</th><th>名称</th><th>糖</th><th>骨架</th><th>O-Me</th><th>分支</th><th>状态</th><th>离子</th></tr></thead><tbody>
      ${r.items.map(x => `<tr><td class="mono">${esc(x.source_row_id)}</td><td>${esc(x.derivative_name_raw)}</td>
      <td>${esc(x.parent_sugar || '—')}</td><td>${esc(x.skeleton || '—')}</td>
      <td class="mono">${esc((x.o_methyl_positions || []).join(',') || '—')}</td>
      <td>${x.branch === 'neutral_aldose' ? '<span class="tag">中性醛糖</span>' : `<span class="tag" style="color:var(--warn)">${esc(x.branch)}</span>`}</td>
      <td>${badge(x.review_status)}</td><td>${x.ion_count}</td></tr>`).join('')}
    </tbody></table></div>
  </div>
  <div class="card">
    <h2>化学实体与多来源</h2>
    <div class="scroll"><table><thead><tr><th>实体</th><th>O-Me</th><th>O-Ac</th><th>来源数</th><th>全部原残基来源</th></tr></thead><tbody>
      ${d.items.map(x => `<tr><td class="mono">${esc(x.derivative_id)}</td>
      <td class="mono">${esc((x.o_methyl_positions || []).join(',') || '—')}</td>
      <td class="mono">${esc((x.o_acetyl_positions || []).join(','))}</td>
      <td>${x.origins.length}${x.multi_origin ? ' <span class="badge b-warn">多来源</span>' : ''}</td>
      <td>${x.origins.map(o => `<span class="tag">${esc(o.linkage_positions.join(',') || 't')}-${esc(o.ring_form)}${o.parent_sugar ? ' ' + esc(o.parent_sugar) : ''}</span>`).join('')}</td>
      </tr>`).join('')}
    </tbody></table></div>
  </div>`;
}

/* ============================================================ LLM 配置 */
async function viewProviders() {
  const r = await api('/llm/providers');
  $('#main').innerHTML = `
  <div class="card">
    <h2>LLM 服务商配置</h2>
    <p class="hint">密钥只存服务端引用（环境变量名），数据库不保存明文；本接口永不回传密钥。</p>
    <div class="grid g3">
      <div class="field"><label>配置名称</label><input id="pv_name" placeholder="lab_default_v1"></div>
      <div class="field"><label>适配器</label><select id="pv_ad"><option value="mock">mock（离线）</option><option value="openai_chat">openai_chat</option><option value="openai_responses">openai_responses</option></select></div>
      <div class="field"><label>Model ID</label><input id="pv_model" placeholder="gpt-4o-mini"></div>
      <div class="field"><label>Base URL</label><input id="pv_url" placeholder="https://api.openai.com/v1"></div>
      <div class="field"><label>密钥引用（环境变量名）</label><input id="pv_ref" placeholder="OPENAI_API_KEY"></div>
    </div>
    <div class="row"><button class="btn" id="pv_add">新增配置</button></div>
    <div id="pv_out"></div>
  </div>
  <div class="card"><h2>已配置服务商</h2><div id="pv_list"></div></div>`;

  const render = async () => {
    const rs = await api('/llm/providers');
    $('#pv_list').innerHTML = `<table><thead><tr><th>ID</th><th>名称</th><th>适配器</th><th>模型</th><th>配置版本</th><th>密钥</th><th>操作</th></tr></thead><tbody>
      ${rs.items.map(p => `<tr><td class="mono">${esc(p.provider_id)}</td><td>${esc(p.name)}</td><td>${esc(p.adapter)}</td>
      <td class="mono">${esc(p.model || '—')}</td><td>v${p.config_version}</td>
      <td>${p.key_present ? '<span class="badge b-ok">已配置</span>' : '<span class="badge b-mute">未设置</span>'}</td>
      <td><button class="btn sec" data-test="${esc(p.provider_id)}">能力测试</button></td></tr>`).join('')}</tbody></table>`;
    document.querySelectorAll('[data-test]').forEach(b => b.onclick = async () => {
      const t = await api(`/llm/providers/${b.dataset.test}/test`, { method: 'POST' });
      $('#pv_out').innerHTML = `<div class="note">结构化输出：<b>${esc(String(t.structured_output))}</b> · 密钥：${t.key_present ? '已配置' : '未设置'}
        ${t.warning ? `<br><span style="color:var(--warn)">${esc(t.warning)}</span>` : ''}<br>${esc(t.note)}</div>`;
    });
  };
  $('#pv_add').onclick = async () => {
    try { await api('/llm/providers', { method: 'POST', body: {
      name: $('#pv_name').value || 'unnamed', adapter: $('#pv_ad').value,
      base_url: $('#pv_url').value || null, model: $('#pv_model').value || null,
      secret_ref: $('#pv_ref').value || null } }); toast('已新增'); render(); }
    catch (e) { toast(e.message, true); }
  };
  render();
}

/* ============================================================ RI 标准库（保留指数定种通道）
   数据驱动：在这里填标准品 RI（或 RT）即可让第 5 步的 RI 定种通道生效；
   表为空时该通道完全静默，不改变现有结论。填表即生效，无需改任何逻辑。
-------------------------------------------------------------------------- */
async function viewRIStd() {
  $('#main').innerHTML = `
  <div class="card">
    <h2>RI 标准库（保留指数定种通道）</h2>
    <p class="hint">EI 碎片只取决于取代基位置，<b>无法区分同一骨架内的差向异构体</b>（Glc/Man/Gal、Rha/Fuc…）。
      唯一能把糖种分开的维度是气相色谱保留行为——即<b>特定柱子 + 特定升温程序</b>下的保留指数。
      在这里按 (方法, 骨架, O-甲基位点) 填标准品 RI，第 5 步即可把糖种从骨架级收窄到糖种级。
      <b>表为空时该通道静默，不影响任何现有结果。</b></p>
    <div class="grid g3">
      <div class="field"><label>方法 ID *</label><input id="ri_method" placeholder="如 HP-5MS_v1"></div>
      <div class="field"><label>骨架 *</label><select id="ri_skel"><option>Hex</option><option>dHex</option><option>Pen</option><option>HexN</option><option>HexA</option></select></div>
      <div class="field"><label>O-甲基位点 *</label><input id="ri_me" placeholder="如 2,3,4,6"></div>
      <div class="field"><label>糖种 *</label><input id="ri_sugar" placeholder="如 Glc"></div>
      <div class="field"><label>保留指数 RI</label><input id="ri_idx" type="number" step="0.1" placeholder="如 2050"></div>
      <div class="field"><label>保留时间 RT/min</label><input id="ri_rt" type="number" step="0.01" placeholder="如 23.4"></div>
      <div class="field"><label>来源标注</label><input id="ri_src" placeholder="in-house standard / 文献"></div>
    </div>
    <div class="note" style="font-size:12px">RI 与 RT 至少填其一；同 (方法, 骨架, 甲基位点, 糖种) 视为同一条目，重复提交会覆盖。<code>ri_tolerance=15</code> / <code>ri_wide_tolerance=30</code> 为精确/放宽容差（可在后端 config 调）。</div>
    <div class="row"><button class="btn" id="ri_add">新增标准</button></div>
    <div id="ri_out"></div>
  </div>
  <div class="card">
    <h2>当前标准表</h2>
    <p class="hint">写入 <code>data/ri_standards.json</code>，立即生效。可用 <code>data/ri_standards.example.json</code> 作模板。</p>
    <div id="ri_list" class="scroll"></div>
  </div>`;

  const render = async () => {
    try {
      const r = await api('/ri-standards');
      $('#ri_list').innerHTML = r.items.length ? `<table><thead><tr>
        <th>方法</th><th>骨架</th><th>O-Me</th><th>糖种</th><th>RI</th><th>RT</th><th>来源</th></tr></thead><tbody>
        ${r.items.map(s => `<tr><td class="mono">${esc(s.method_id)}</td><td>${esc(s.skeleton)}</td>
          <td class="mono">${(s.o_methyl_positions || []).join(',') || '—'}</td>
          <td>${esc(s.parent_sugar)}</td><td>${s.retention_index ?? '—'}</td>
          <td>${s.retention_time ?? '—'}</td><td style="font-size:12px;color:var(--text-2)">${esc(s.source || '—')}</td></tr>`).join('')}
      </tbody></table>`
        : '<div class="empty">标准表为空，RI 定种通道静默（no-op）。填一条标准即生效。</div>';
    } catch (e) { $('#ri_list').innerHTML = `<div class="note danger">${esc(e.message)}</div>`; }
  };

  $('#ri_add').onclick = async () => {
    try {
      const method = $('#ri_method').value.trim();
      const sugar = $('#ri_sugar').value.trim();
      const me = $('#ri_me').value.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
      const idx = $('#ri_idx').value ? +$('#ri_idx').value : null;
      const rt = $('#ri_rt').value ? +$('#ri_rt').value : null;
      if (!method || !sugar) throw new Error('方法 ID 与糖种必填');
      if (idx == null && rt == null) throw new Error('保留指数 RI 与保留时间 RT 至少填其一');
      const r = await api('/ri-standards', { method: 'POST', body: {
        method_id: method, skeleton: $('#ri_skel').value, o_methyl_positions: me,
        parent_sugar: sugar, retention_index: idx, retention_time: rt, source: $('#ri_src').value || '' } });
      $('#ri_out').innerHTML = `<div class="note">已写入标准：<code>${esc(r.standard.parent_sugar)}</code> @ ${esc(r.standard.retention_index ?? r.standard.retention_time)}，共 ${r.count} 条。</div>`;
      toast('标准已生效'); render();
    } catch (e) { toast(e.message, true); }
  };
  render();
}

/* ============================================================ 启动 */
async function refreshFooter() {
  try {
    const h = await api('/health'); $('#ruleVersion').textContent = h.rule_version;
    const r = await api('/references'); $('#refCount').textContent = r.count;
    const d = await api('/derivatives'); $('#derivCount').textContent = d.count;
    $('#healthPill').textContent = '服务正常'; $('#healthPill').className = 'pill';
  } catch (e) { $('#healthPill').textContent = '服务未连接'; $('#healthPill').className = 'pill bad'; }
}

/* 步骤条是动态渲染的，用事件委托而不是一次性绑定 */
document.addEventListener('click', e => {
  const b = e.target.closest('.step');
  if (b && b.dataset.view) go(b.dataset.view);
});
refreshFooter();
go('sample');
