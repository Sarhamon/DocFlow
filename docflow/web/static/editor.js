// 정보 입력 화면 (M8): 서식 모양 그대로 칸을 채우고, 칸마다 점검하고, 검증·문서 만들기까지.
//
// 입력값은 API 의 입력 JSON 과 같은 모양이다:
//   { variant, fields: {이름: 값}, cells: {t0.r1.c2: 값}, rows: {t0.r7-16: [{열번호: 값}]}, replace: {문구: 문구} }
// 칸마다 data-where 를 붙인다. 검증 결과의 where 와 같은 문자열이라 오류에서 칸으로 바로 간다.
"use strict";

(() => {
  const { h, api } = DF;
  const docId = DF.params.doc_id;
  const SAVE_DELAY = 1200;

  let doc;        // 문서 요약
  let schema;     // 서식 스키마 (문서가 묶인 버전)
  let layout;     // 표 배치
  let fieldsMeta; // 누름틀 목록 (한글 이름·필수)
  let raw;        // 입력값
  const prefill = new Map(); // 반복영역 id -> 새 행 기본값 {열번호: 값}
  let issues = [];
  let severity = "";
  let saveTimer = null;
  let saving = Promise.resolve();
  let dirty = false;

  const readonly = () => doc.status !== "draft";
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- 입력값
  function variantIndex() {
    if (!DF.choices(schema).length) return schema.variant ?? null;
    const v = schema.variants.find((x) => x.label === raw.variant || String(x.index) === String(raw.variant));
    return v ? v.index : schema.variants[0].index;
  }

  /** 반복영역 열 키(번호 또는 열 이름)를 열 번호로. */
  function columnOf(rep, key) {
    const inputs = rep.columns.filter((c) => c.role === "input");
    if (/^\d+$/.test(key)) return Number(key);
    const exact = inputs.filter((c) => c.label === key);
    const part = inputs.filter((c) => c.label.includes(key));
    const found = exact.length ? exact : part;
    return found.length === 1 ? found[0].col : null;
  }

  function byColumn(rep, line) {
    const row = {};
    for (const [key, v] of Object.entries(line || {})) {
      const col = columnOf(rep, key);
      if (col !== null && !key.startsWith("_")) row[String(col)] = String(v);
    }
    return row;
  }

  function normalize(input, skeleton) {
    const src = Object.keys(input || {}).length ? input : skeleton;
    const out = { fields: {}, cells: {}, rows: {}, replace: {} };
    if (src.variant !== undefined) out.variant = src.variant;
    else if (DF.choices(schema).length) out.variant = schema.variants[0].label;
    for (const k of ["fields", "cells", "replace"]) {
      for (const [key, v] of Object.entries(src[k] || {})) if (!key.startsWith("_")) out[k][key] = String(v);
    }
    for (const rep of schema.repeats) {
      // 틀의 첫 행이 새 행의 기본값이다 (연번·기본 금액. 날짜 자리표시자는 틀에서 이미 뺐다)
      prefill.set(rep.id, byColumn(rep, ((skeleton.rows || {})[rep.id] || [])[0]));
      const lines = (src.rows || {})[rep.id] || (skeleton.rows || {})[rep.id] || [];
      out.rows[rep.id] = lines.map((line) => byColumn(rep, line));
    }
    return out;
  }

  /** 새 행: 서식에 미리 적힌 값(연번·기본 금액)을 넣는다. */
  function newRow(rep, index) {
    const row = {};
    const base = prefill.get(rep.id) || {};
    for (const c of rep.columns) {
      const v = base[String(c.col)];
      if (c.role !== "input" || !v) continue;
      row[String(c.col)] = /^\d+$/.test(v) && c.input_type === "number" ? String(index + 1) : v;
    }
    return row;
  }

  /** 저장할 입력값. 빈 값은 뺀다(필수 누락은 검증이 잡는다). */
  function payload() {
    const clean = (obj) => Object.fromEntries(Object.entries(obj).filter(([, v]) => String(v).trim() !== ""));
    const out = { fields: clean(raw.fields), cells: clean(raw.cells), rows: {}, replace: {} };
    if (raw.variant !== undefined) out.variant = raw.variant;
    for (const [id, lines] of Object.entries(raw.rows)) {
      const rows = lines.map(clean);
      while (rows.length && !Object.keys(rows[rows.length - 1]).length) rows.pop();
      if (rows.length) out.rows[id] = rows;
    }
    for (const [k, v] of Object.entries(raw.replace)) if (k.trim()) out.replace[k] = v;
    return out;
  }

  // ---------------------------------------------------------------- 저장 (IN_12)
  function setSaveState(text) { $("save-state").textContent = text; }

  function changed() {
    if (readonly()) return;
    dirty = true;
    setSaveState("저장 대기…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, SAVE_DELAY);
  }

  function save() {
    clearTimeout(saveTimer);
    if (!dirty || readonly()) return saving;
    dirty = false;
    setSaveState("저장 중…");
    saving = saving.then(async () => {
      try {
        const d = await api("PUT", `/api/documents/${docId}/input`, payload());
        doc.updated_at = d.updated_at;
        setSaveState(`저장됨 ${DF.fmtTime(d.updated_at).slice(11)}`);
      } catch (e) {
        dirty = true;
        setSaveState("저장 실패");
        DF.fail(e);
      }
    });
    return saving;
  }

  window.addEventListener("beforeunload", (ev) => {
    if (dirty) { save(); ev.preventDefault(); ev.returnValue = ""; }
  });

  // ---------------------------------------------------------------- 실시간 점검 (IN_14~19)
  let checkTimer = null;
  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(check, 300);
  }

  async function check() {
    if (readonly()) return;
    try {
      const r = await api("POST", `/api/documents/${docId}/check`, payload());
      showIssues(r.issues);
    } catch (e) { DF.fail(e); }
  }

  function showIssues(list, note) {
    issues = list;
    markCells();
    const errors = list.filter((i) => i.severity === "error").length;
    const warns = list.length - errors;
    $("issue-count").textContent = list.length ? `오류 ${errors} · 경고 ${warns}` : "";
    const ul = $("issues");
    const shown = list.filter((i) => !severity || i.severity === severity);
    if (!list.length) {
      ul.replaceChildren(h("li", { class: "msg ok", style: "cursor:default" }, note || "걸린 것이 없습니다."));
      return;
    }
    // 오류 목록 (ER_01~03): 항목, 사유, 조치, 이동 (ED_01)
    ul.replaceChildren(...shown.map((i) => h("li", { class: i.severity, onclick: () => focusWhere(i.where), title: "눌러서 칸으로 이동" },
      h("div", { class: "where" }, `${i.severity === "error" ? "오류" : "경고"} · ${placeName(i)}`),
      h("div", {}, i.message),
      i.action ? h("div", { class: "action" }, `→ ${i.action}`) : null)));
    if (note) ul.prepend(h("li", { class: `msg ${errors ? "error" : "warn"}`, style: "cursor:default" }, note));
  }

  function placeName(i) {
    const loc = i.location;
    if (loc.kind === "field") {
      const f = fieldsMeta.find((x) => x.name === loc.id);
      return f ? f.label : loc.id;
    }
    const where = `표 ${loc.table + 1}`;
    if (loc.kind === "repeat") return `${where} 명단 ${loc.index + 1}번째 행 '${loc.label}'`;
    return `${where} '${loc.label || i.where}'`;
  }

  function cellOf(where) {
    return document.querySelector(`[data-where="${CSS.escape(where)}"]`);
  }

  function markCells() {
    document.querySelectorAll(".cell-icon").forEach((x) => x.remove());
    document.querySelectorAll(".has-issue, .has-warning").forEach((x) => x.classList.remove("has-issue", "has-warning"));
    for (const i of issues) {
      const el = cellOf(i.where);
      if (!el) continue;
      const box = el.closest("td, .input-wrap") || el;
      box.classList.add(i.severity === "error" ? "has-issue" : "has-warning");
      if (!box.querySelector(".cell-icon")) {
        box.append(h("span", { class: `cell-icon ${i.severity}`, title: `${i.message}\n${i.action || ""}` }, "!"));
      }
    }
  }

  function focusWhere(where) {
    const el = cellOf(where);
    if (!el) { DF.toast("이 칸은 지금 화면에 없습니다 (다른 변형이거나 지운 행)."); return; }
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    const target = el.matches("input, textarea, select") ? el : el.querySelector("input, textarea");
    if (target) setTimeout(() => target.focus({ preventScroll: true }), 250);
    const box = el.closest("td, .input-wrap") || el;
    box.classList.remove("flash");
    void box.offsetWidth;
    box.classList.add("flash");
  }

  // ---------------------------------------------------------------- 입력기
  function formatAmount(v) {
    const t = v.replace(/[,\s]/g, "");
    return /^-?\d+$/.test(t) ? Number(t).toLocaleString("ko-KR") : v;
  }

  const HINT = { date: "2026.10.12", amount: "0", number: "숫자" };

  /** 칸 하나의 입력기. set 으로 값을 돌려준다. */
  function input(where, type, value, placeholder, set, { multiline = false } = {}) {
    if (type === "checkbox") {
      const box = checkbox(where, value, placeholder, set);
      if (box) return box;
    }
    const hint = placeholder ? placeholder.replace(/\s+/g, " ").trim() : (HINT[type] || "");
    const attrs = {
      dataset: { where }, value: value || "", placeholder: hint, title: hint ? `예: ${hint}` : null,
      class: type === "amount" ? "amount" : "",
      inputmode: type === "amount" || type === "number" ? "numeric" : null,
      oninput: (ev) => { set(ev.target.value); changed(); },
      onchange: () => scheduleCheck(),
      onblur: (ev) => {
        if (type === "amount" && ev.target.value) {
          const f = formatAmount(ev.target.value);
          if (f !== ev.target.value) { ev.target.value = f; set(f); changed(); }
        }
        scheduleCheck();
      },
    };
    if (multiline) return h("textarea", { ...attrs, rows: 3 });
    return h("input", attrs);
  }

  /** □가 붙은 선택지 칸 (`□현장견학 □현장체험 □기타( )`). 선택지가 없으면 null. */
  function checkbox(where, value, placeholder, set) {
    const options = (placeholder || "").split(/[□■☐☑]/).map((x) => x.trim()).filter(Boolean);
    if (options.length < 1 || !/[□■☐☑]/.test(placeholder)) return null;
    const box = h("div", { class: "checks", dataset: { where } });
    const state = options.map((o) => {
      const base = o.replace(/\(\s*\)$/, "").trim();
      const free = /\(\s*\)$/.test(o);
      const m = (value || "").match(new RegExp(`■\\s*${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:\\(([^)]*)\\))?`));
      return { base, free, on: !!m, text: m && m[1] ? m[1].trim() : "" };
    });
    const write = () => {
      const any = state.some((s) => s.on);
      // 하나도 고르지 않으면 빈 칸으로 둔다: 필수 칸이면 검증이 알려 준다
      set(any ? state.map((s) => `${s.on ? "■" : "□"}${s.base}${s.free ? `(${s.text})` : ""}`).join(" ") : "");
      changed(); scheduleCheck();
    };
    for (const s of state) {
      const cb = h("input", { type: "checkbox", checked: s.on, onchange: (ev) => { s.on = ev.target.checked; write(); } });
      const label = h("label", {}, cb, s.base);
      if (s.free) label.append(h("input", { type: "text", value: s.text, "aria-label": `${s.base} 내용`, oninput: (ev) => { s.text = ev.target.value; s.on = true; cb.checked = true; write(); } }));
      box.append(label);
    }
    return box;
  }

  // ---------------------------------------------------------------- 기본 항목 (누름틀, IN_01~03)
  function drawFields() {
    const grid = $("fields");
    grid.replaceChildren();
    $("fields-card").classList.toggle("hidden", !fieldsMeta.length);
    for (const f of fieldsMeta) {
      const multiline = f.name === "content_body" || (f.placeholder || "").includes("\n");
      grid.append(
        h("div", { class: `lab${f.required ? " required" : ""}` }, f.label, f.label !== f.name ? h("div", { class: "hint" }, f.name) : null),
        h("div", { class: "input-wrap" },
          input(f.name, f.input_type, raw.fields[f.name], f.placeholder, (v) => { raw.fields[f.name] = v; }, { multiline }),
          f.placeholder && f.required ? h("div", { class: "hint" }, `서식 예시: ${f.placeholder.split("\n")[0].slice(0, 80)}`) : null),
      );
    }
  }

  // ---------------------------------------------------------------- 표 (IN_04~11, IN_18)
  function drawTables() {
    const box = $("tables");
    box.replaceChildren();
    const idx = DF.indexSchema(schema);
    const variant = variantIndex();
    let shown = 0;
    let quiet = 0;
    for (const t of layout) {
      if (!DF.inVariant(schema, t.index, variant)) continue;
      const own = idx.has(t.index) ? idx.get(t.index) : null;
      const inputs = own && ([...own.slots.values()].some((s) => s.role === "input") || own.repeats.length);
      if (!inputs) { quiet++; continue; }
      shown++;
      box.append(h("div", { class: "sheet" }, h("div", { class: "muted small" }, `표 ${t.index + 1}`), drawTable(t, own)));
      for (const rep of own.repeats) box.append(repeatTools(rep));
    }
    if (!shown) box.append(h("p", { class: "muted" }, "이 서식에는 표에 채울 칸이 없습니다."));
    if (quiet) box.append(h("p", { class: "hint" }, `채울 칸이 없는 표 ${quiet}개는 서식 그대로 들어갑니다.`));
  }

  function repeatOf(own, r) {
    return own.repeats.find((rep) => rep.start_row <= r && r <= rep.end_row);
  }

  function sumColumn(rep, col) {
    let total = 0;
    let n = 0;
    for (const row of raw.rows[rep.id] || []) {
      const t = String(row[String(col)] || "").replace(/[,\s원₩\\]/g, "");
      if (/^-?\d+$/.test(t)) { total += Number(t); n++; }
    }
    return n ? total : null;
  }

  function drawTable(t, own) {
    const skip = new Set();
    for (const rep of own.repeats) {
      for (let r = rep.start_row; r <= rep.end_row; r++) skip.add(r);
      for (const r of rep.example_rows) skip.add(r);
    }
    const totalRows = new Map(own.repeats.filter((r) => r.total_row != null).map((r) => [r.total_row, r]));
    return DF.renderTable(t, {
      skipRow: (r) => skip.has(r),
      rowsAt: (r) => {
        const rep = own.repeats.find((x) => x.start_row === r);
        return rep ? repeatRows(t, rep) : null;
      },
      cell: (c) => {
        const key = `${c.row},${c.col}`;
        const slot = own.slots.get(key);
        if (slot) {
          if (slot.role === "fixed") return null; // 관리자가 입력폼에서 뺀 칸: 서식 글자 그대로
          if (slot.role === "approval") return { node: c.text.trim() ? c.text : "결재란", cls: "approval" };
          if (slot.role === "computed") return { node: "자동 계산", cls: "computed" };
          const multiline = slot.input_type === "text" && (slot.placeholder.includes("\n") || slot.placeholder.length > 40);
          return {
            node: input(slot.id, slot.input_type, raw.cells[slot.id], slot.placeholder, (v) => { raw.cells[slot.id] = v; }, { multiline }),
            cls: `slot${slot.required ? " required" : ""}`,
          };
        }
        const formula = own.formulas.get(key);
        if (formula) {
          // 합계 행 수식: 입력한 행의 합을 미리 보여 준다 (IN_18). 실제 값은 문서를 만들 때 다시 계산한다
          const rep = totalRows.get(c.row);
          const sum = rep ? sumColumn(rep, c.col) : null;
          return { node: h("span", { title: `수식 ${formula.formula} — 문서를 만들 때 계산합니다` }, sum !== null ? sum.toLocaleString("ko-KR") : "자동 계산"), cls: "computed" };
        }
        return null;
      },
    });
  }

  /** 반복영역의 입력 행들. 서식 첫 데이터 행의 칸 모양을 그대로 쓴다. */
  function repeatRows(t, rep) {
    const lines = raw.rows[rep.id] || (raw.rows[rep.id] = []);
    if (!lines.length) lines.push(newRow(rep, 0));
    const anchors = t.cells.filter((c) => c.row === rep.start_row).sort((a, b) => a.col - b.col);
    const cols = new Map(rep.columns.map((c) => [c.col, c]));
    return lines.map((line, i) => {
      const tr = h("tr", { class: `data${i >= rep.capacity ? " over" : ""}` });
      let at = 0;
      anchors.forEach((a, k) => {
        if (a.col > at) tr.append(h("td", { colspan: a.col - at > 1 ? a.col - at : null }));
        const spec = cols.get(a.col);
        const td = h("td", { colspan: a.col_span > 1 ? a.col_span : null, style: "position:relative" });
        if (spec && spec.role === "input") {
          const where = `${rep.id}[${i}].c${spec.col}`;
          // 틀에서 뺀 서식 예시(`2000.00.00(금)`)는 칸 안내로 보여 준다
          const hint = spec.prefilled && !(prefill.get(rep.id) || {})[String(spec.col)] ? spec.prefilled : "";
          const el = input(where, spec.input_type, line[String(spec.col)], hint, (v) => { line[String(spec.col)] = v; refreshSums(rep); }, {});
          el.setAttribute("aria-label", `${i + 1}번째 행 ${spec.label}`);
          el.addEventListener("paste", (ev) => paste(ev, rep, i, spec.col));
          td.append(el);
        } else if (spec && spec.role === "computed") {
          td.className = "computed";
          td.append("자동");
        }
        if (k === 0) {
          td.prepend(h("button", { type: "button", class: "link small", title: `${i + 1}번째 행 지우기`, style: "position:absolute;left:-2px;top:-2px;font-size:11px", onclick: () => removeRow(rep, i) }, "×"));
        }
        tr.append(td);
        at = a.col + a.col_span;
      });
      if (at < t.cols) tr.append(h("td", { colspan: t.cols - at > 1 ? t.cols - at : null }));
      return tr;
    });
  }

  function refreshSums(rep) {
    clearTimeout(refreshSums.timer);
    refreshSums.timer = setTimeout(() => {
      const el = document.querySelector(`[data-sums="${CSS.escape(rep.id)}"]`);
      if (el) el.replaceWith(sums(rep));
    }, 200);
  }

  function sums(rep) {
    const parts = rep.columns.filter((c) => c.input_type === "amount" && c.role === "input")
      .map((c) => { const s = sumColumn(rep, c.col); return s === null ? null : h("span", {}, `${c.label} 합 `, h("b", {}, `${s.toLocaleString("ko-KR")}원`)); })
      .filter(Boolean);
    return h("div", { class: "sums", dataset: { sums: rep.id } }, ...parts.flatMap((p, i) => (i ? [" · ", p] : [p])));
  }

  function repeatTools(rep) {
    const n = (raw.rows[rep.id] || []).length;
    const tools = h("div", { class: "repeat-tools" },
      h("button", { type: "button", class: "small", onclick: () => addRow(rep) }, "+ 행 추가"),
      h("span", {}, `${n}행 입력 · 서식에는 ${rep.capacity}행`),
      // 서식 행 수 초과 경고 (IN_10)
      n > rep.capacity ? h("span", { class: "badge warning" }, `서식 행 수를 넘었습니다. 문서를 만들 때 ${n - rep.capacity}행을 늘립니다`) : null,
      rep.has_example ? h("span", {}, "서식의 예시 행(홍길동 등)은 첫 행 값으로 바뀝니다") : null,
      h("span", {}, "엑셀에서 여러 칸을 복사해 붙여 넣을 수 있습니다"));
    return h("div", {}, tools, sums(rep));
  }

  function addRow(rep) {
    const lines = raw.rows[rep.id];
    lines.push(newRow(rep, lines.length));
    redraw();
    changed();
    const last = cellOf(`${rep.id}[${lines.length - 1}].c${rep.columns.find((c) => c.role === "input").col}`);
    if (last) last.focus();
  }

  function removeRow(rep, i) {
    const lines = raw.rows[rep.id];
    const base = prefill.get(rep.id) || {};
    const serial = (col) => rep.columns.some((c) => String(c.col) === col && c.input_type === "number" && /^\d+$/.test(base[col] || ""));
    const filled = Object.entries(lines[i]).some(([col, v]) => v.trim() && v !== base[col] && !serial(col));
    if (filled && !confirm(`${i + 1}번째 행을 지울까요?`)) return;
    lines.splice(i, 1);
    // 연번은 다시 매긴다
    const seq = rep.columns.find((c) => c.role === "input" && c.input_type === "number" && c.prefilled && /^\d+$/.test(c.prefilled));
    if (seq) lines.forEach((l, k) => { if (/^\d*$/.test(l[String(seq.col)] || "")) l[String(seq.col)] = String(k + 1); });
    redraw();
    changed();
    scheduleCheck();
  }

  /** 엑셀 붙여넣기 (IN_09): 탭·줄바꿈으로 나뉜 값을 이 칸부터 오른쪽·아래로 채운다. */
  function paste(ev, rep, rowIndex, col) {
    const text = (ev.clipboardData || window.clipboardData).getData("text");
    if (!/[\t\n]/.test(text.replace(/\r?\n$/, ""))) return;
    ev.preventDefault();
    const grid = text.replace(/\r/g, "").replace(/\n$/, "").split("\n").map((l) => l.split("\t"));
    const inputs = rep.columns.filter((c) => c.role === "input").map((c) => c.col);
    const start = inputs.indexOf(col);
    const lines = raw.rows[rep.id];
    grid.forEach((cells, i) => {
      const at = rowIndex + i;
      while (lines.length <= at) lines.push(newRow(rep, lines.length));
      cells.forEach((v, j) => {
        const target = inputs[start + j];
        if (target !== undefined) lines[at][String(target)] = v.trim();
      });
    });
    redraw();
    changed();
    scheduleCheck();
    DF.toast(`${grid.length}행을 붙여 넣었습니다.`);
  }

  // ---------------------------------------------------------------- 본문 문구 바꾸기
  function drawReplace() {
    const box = $("replace");
    box.replaceChildren();
    const entries = Object.entries(raw.replace);
    entries.forEach(([from, to], i) => {
      const fromEl = h("input", { value: from, placeholder: "찾을 문구", "aria-label": "찾을 문구" });
      const toEl = h("input", { value: to, placeholder: "바꿀 문구", "aria-label": "바꿀 문구" });
      const sync = () => {
        const next = Object.entries(raw.replace);
        next[i] = [fromEl.value, toEl.value];
        raw.replace = Object.fromEntries(next);
        changed();
      };
      fromEl.addEventListener("change", sync);
      toEl.addEventListener("input", sync);
      box.append(h("div", { class: "replace-row" }, fromEl, toEl,
        h("button", { type: "button", class: "small", onclick: () => { delete raw.replace[from]; drawReplace(); changed(); } }, "지우기")));
    });
  }

  function addReplace(from, to) {
    if (from in raw.replace && raw.replace[from] === to) return;
    raw.replace[from] = to;
    drawReplace();
    changed();
  }

  // ---------------------------------------------------------------- 그리기
  function redraw() {
    drawFields();
    drawTables();
    drawReplace();
    markCells();
  }

  function drawHead() {
    $("title").value = doc.title;
    const st = $("status");
    st.textContent = DF.STATUS[doc.status] || doc.status;
    st.className = `badge ${doc.status}`;
    document.title = `${doc.title} · DocFlow`;
    $("form-area").disabled = readonly();
    $("form-area").classList.toggle("ro", readonly());
    $("btn-validate").disabled = readonly();
    $("btn-generate").disabled = readonly();
    $("replace-card").classList.toggle("hidden", readonly() && !Object.keys(raw.replace).length);
    const banner = $("banner");
    banner.replaceChildren();
    if (readonly()) {
      banner.append(h("div", { class: "msg info readonly-note row" },
        h("span", {}, `완료된 문서입니다 (${doc.revision}판). 고치려면 복제해서 새 문서로 씁니다.`),
        h("span", { class: "spacer" }),
        downloads(doc.files),
        h("button", { type: "button", class: "primary small", onclick: copyDoc }, "복제해서 고치기")));
    }
  }

  function downloads(files, revision) {
    const q = revision ? `?revision=${revision}` : "";
    return h("span", { class: "downloads" }, ...files.map((f) => h("a", { class: "btn small", href: `/api/documents/${docId}/files/${f}${q}` }, `${f} 받기`)));
  }

  async function copyDoc() {
    try {
      const copy = await api("POST", `/api/documents/${docId}/copy`);
      location.href = `/documents/${copy.id}`;
    } catch (e) { DF.fail(e); }
  }

  // ---------------------------------------------------------------- 검증·만들기 (VA_01, ER_07~09)
  async function validateAll() {
    await save();
    const r = await api("POST", `/api/documents/${docId}/validate`);
    const errors = r.issues.filter((i) => i.severity === "error").length;
    const note = errors ? `오류 ${errors}건을 고쳐야 문서를 만들 수 있습니다.` : r.issues.length ? `오류는 없고 경고 ${r.issues.length}건이 있습니다. 확인 후 만들 수 있습니다.` : "검증을 통과했습니다. 문서를 만들 수 있습니다.";
    showIssues(r.issues, note);
    return r;
  }

  async function generate() {
    try {
      const r = await validateAll();
      const errors = r.issues.filter((i) => i.severity === "error");
      if (errors.length) {
        // 오류가 있으면 첫 오류 칸으로 (ER_08)
        focusWhere(errors[0].where);
        DF.toast(`오류 ${errors.length}건을 먼저 고치세요.`, "error");
        return;
      }
      const warn = $("gen-warn");
      warn.replaceChildren();
      if (r.issues.length) warn.append(h("div", { class: "msg warn" }, `경고 ${r.issues.length}건이 남아 있습니다. 내용을 확인했으면 그대로 만들 수 있습니다.`));
      $("gen-dialog").showModal();
    } catch (e) { DF.fail(e); }
  }

  async function generateGo() {
    const formats = [...document.querySelectorAll("#gen-dialog input[name=fmt]:checked")].map((x) => x.value);
    const btn = $("gen-go");
    btn.disabled = true;
    btn.textContent = "만드는 중…";
    try {
      const out = await api("POST", `/api/documents/${docId}/generate`, { formats });
      $("gen-dialog").close();
      doc = out.document;
      showResult(out);
      drawHead();
      DF.toast(doc.status === "completed" ? "문서를 만들었습니다." : "문서를 만들었지만 일부 변환에 실패했습니다.", doc.status === "completed" ? "" : "error");
    } catch (e) {
      if (e.status === 422 && e.body.issues) showIssues(e.body.issues, e.message);
      DF.fail(e);
    } finally {
      btn.disabled = false;
      btn.textContent = "만들기";
    }
  }

  function showResult(out) {
    const box = $("result");
    box.replaceChildren();
    $("result-card").classList.remove("hidden");
    const files = out.document.files;
    box.append(h("p", {}, `${out.revision}판을 만들었습니다.`), downloads(files, out.revision));
    const failed = Object.entries(out.conversion_errors);
    if (failed.length) {
      box.append(h("div", { class: "msg error", style: "margin-top:12px" },
        h("div", {}, "변환하지 못한 형식이 있습니다. HWPX 는 보존했고, 문서는 작성 중으로 남았습니다. 다시 [문서 만들기]를 누르면 다음 판으로 만듭니다."),
        h("ul", {}, ...failed.map(([f, why]) => h("li", {}, `${f}: ${why}`)))));
    }
    if (out.leftovers.length) {
      const texts = [...new Set(out.leftovers.map((x) => x.text.trim()).filter(Boolean))];
      box.append(h("div", { class: "msg warn", style: "margin-top:12px" },
        h("div", {}, `문서에 자리표시자 ${texts.length}종이 남아 있습니다.`),
        h("ul", {}, ...texts.slice(0, 10).map((t) => h("li", {}, t))),
        h("div", { class: "small" }, readonly() ? "고치려면 [복제해서 고치기] 후 '본문 문구 바꾸기'에 넣으세요." : "'본문 문구 바꾸기'에 넣어 바꿀 수 있습니다.")));
    }
    if (out.skipped_formulas.length) {
      box.append(h("div", { class: "msg warn", style: "margin-top:12px" }, `계산하지 못한 수식 ${out.skipped_formulas.length}개는 서식 값 그대로 두었습니다.`));
    }
  }

  // ---------------------------------------------------------------- 시작
  async function load() {
    const [d, f] = await Promise.all([api("GET", `/api/documents/${docId}`), api("GET", `/api/documents/${docId}/form`)]);
    doc = d;
    schema = f.schema;
    layout = f.layout;
    fieldsMeta = f.fields;
    raw = normalize(d.input, f.skeleton);
    if (DF.choices(schema).length) {
      const sel = $("variant");
      sel.replaceChildren(...schema.variants.map((v) => h("option", { value: v.label }, v.label)));
      sel.value = schema.variants.find((v) => v.index === variantIndex()).label;
      sel.addEventListener("change", () => { raw.variant = sel.value; redraw(); changed(); scheduleCheck(); });
      $("variant-card").classList.remove("hidden");
    }
    drawHead();
    redraw();
    setSaveState(`저장됨 ${DF.fmtTime(doc.updated_at).slice(11)}`);
    if (!readonly()) check();
  }

  $("title").addEventListener("change", async (ev) => {
    const title = ev.target.value.trim();
    if (!title) { ev.target.value = doc.title; return; }
    try { doc = { ...doc, ...(await api("PATCH", `/api/documents/${docId}`, { title })) }; drawHead(); DF.toast("문서 이름을 바꿨습니다."); } catch (e) { DF.fail(e); }
  });
  $("btn-validate").addEventListener("click", () => validateAll().catch(DF.fail));
  $("btn-generate").addEventListener("click", generate);
  $("gen-go").addEventListener("click", generateGo);
  $("add-replace").addEventListener("click", () => addReplace("", ""));
  $("add-today").addEventListener("click", () => {
    const d = new Date();
    addReplace("20○○년 ○○월 ○○일", `${d.getFullYear()}년 ${d.getMonth() + 1}월 ${d.getDate()}일`);
  });
  for (const b of document.querySelectorAll("#filters button")) {
    b.addEventListener("click", () => {
      severity = b.dataset.sev;
      document.querySelectorAll("#filters button").forEach((x) => x.classList.toggle("on", x === b));
      showIssues(issues);
    });
  }

  load().catch((e) => {
    $("tables").replaceChildren(h("div", { class: "msg error" }, `문서를 열 수 없습니다: ${e.message}`));
    DF.fail(e);
  });
})();
