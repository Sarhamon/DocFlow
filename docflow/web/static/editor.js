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
  let draftTargets = new Map(); // 본문 초안을 쓸 수 있는 칸: where -> 이름
  let checklist = 0;            // 정성 판정 물음 수 (관리자가 정한다)
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
    // 채택한 초안 칸 -> 인용 근거(고쳐 쓰기 전의 글). 예전 모양(칸 목록)도 읽는다
    const drafted = src._drafted || {};
    out._drafted = Array.isArray(drafted) ? Object.fromEntries(drafted.map((w) => [w, ""])) : { ...drafted };
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
    // 채택한 본문 초안 칸: 점검이 입력값과 숫자가 어긋나는지 계속 본다 (ED_05)
    const drafted = Object.entries(raw._drafted).filter(([w]) => String(valueAt(w)).trim());
    if (drafted.length) out._drafted = Object.fromEntries(drafted);
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
      showIssues([...r.issues, ...liveNotes(), ...reviewIssues()]);
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
    if (loc.kind === "document") return "문서 전체";
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
    if (where === "document") { DF.toast("문서 전체에 대한 판정입니다. 근거를 문서에서 찾지 못했습니다."); return; }
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
          draftButton(f.name),
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
          const field = input(slot.id, slot.input_type, raw.cells[slot.id], slot.placeholder, (v) => { raw.cells[slot.id] = v; }, { multiline });
          const button = draftButton(slot.id);
          return {
            node: button ? h("div", { class: "input-wrap" }, field, button) : field,
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
      h("span", {}, "엑셀에서 여러 칸을 복사해 붙여 넣을 수 있습니다"),
      // 사람 명단이면 입금계좌 확인서로 채운다 (F2-d)
      !readonly() && rep.columns.some((c) => c.role === "input" && PERSON_RE.test(c.label))
        ? h("button", { type: "button", class: "small", onclick: () => pickRoster(rep) }, "입금계좌 확인서로 채우기") : null);
    return h("div", {}, tools, sums(rep));
  }

  /** 사용자가 적은 값이 있는 행인가. 미리 적힌 연번·금액만 있는 행은 덮어써도 된다. */
  function rowFilled(rep, line, i) {
    return Object.entries(line).some(([k, v]) => String(v).trim() && newRow(rep, i)[k] !== v);
  }

  // ---------------------------------------------------------------- 명단 가져오기 (F2-d)
  const PERSON_RE = /성명|예금주|이름|계좌|학번/;
  let rosterRep = null;
  let rosterRows = [];

  function pickRoster(rep) {
    rosterRep = rep;
    const input = $("roster-file");
    input.value = "";
    input.click();
  }

  $("roster-file").addEventListener("change", async (ev) => {
    const files = [...ev.target.files];
    if (!files.length || !rosterRep) return;
    const body = new FormData();
    for (const f of files) body.append("files", f);
    DF.toast(`확인서 ${files.length}개를 읽는 중…`);
    try {
      const out = await DF.upload(`/api/documents/${docId}/roster?repeat=${encodeURIComponent(rosterRep.id)}`, body);
      showRoster(out);
    } catch (e) { DF.fail(e); }
  });

  function showRoster(out) {
    rosterRows = out.rows;
    const ok = out.files.filter((f) => !f.error).length;
    $("roster-files").replaceChildren(
      h("p", { class: "small" }, `파일 ${out.files.length}개 중 ${ok}개를 읽었고 ${out.rows.length}명을 찾았습니다.`,
        out.duplicates ? ` 같은 사람(성명·계좌가 같음) ${out.duplicates}명은 한 번만 넣습니다.` : ""),
      ...out.files.filter((f) => f.error).map((f) => h("div", { class: "msg error small" }, `${f.name}: ${f.error}`)),
      ...out.files.filter((f) => !f.error && !f.people).map((f) => h("div", { class: "msg warn small" }, `${f.name}: 명단 표는 있지만 적힌 사람이 없습니다 (예시 줄만 있음).`)));
    const cols = out.mapping.filter((m) => m.concept);
    const missing = out.mapping.filter((m) => m.concept && !m.found).map((m) => m.label);
    $("roster-map").replaceChildren("채울 열: ", cols.map((m) => m.label).join(", ") || "(없음)",
      missing.length ? h("span", { class: "muted" }, ` — 확인서에 없어 비워 둘 열: ${missing.join(", ")}`) : "");
    $("roster-rows").replaceChildren(
      h("thead", {}, h("tr", {}, ...cols.map((m) => h("th", {}, m.label)))),
      h("tbody", {}, ...out.rows.map((r) => h("tr", {}, ...cols.map((m) => h("td", {}, r[String(m.col)] || ""))))));
    $("roster-append").disabled = $("roster-replace").disabled = !out.rows.length;
    $("roster-dialog").showModal();
  }

  function applyRoster(replace) {
    const rep = rosterRep;
    let lines = raw.rows[rep.id] || [];
    lines = replace ? [] : lines.filter((line, i) => rowFilled(rep, line, i));
    for (const row of rosterRows) lines.push({ ...newRow(rep, lines.length), ...row });
    raw.rows[rep.id] = lines;
    $("roster-dialog").close();
    redraw(); changed(); scheduleCheck();
    DF.toast(`${rosterRows.length}명을 넣었습니다. 학번 등 확인서에 없는 칸을 채우세요.`);
  }
  $("roster-append").addEventListener("click", () => applyRoster(false));
  $("roster-replace").addEventListener("click", () => {
    if (confirm("지금 명단을 지우고 확인서의 명단으로 바꿀까요?")) applyRoster(true);
  });

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

  // ---------------------------------------------------------------- 글로 채우기 (F2-c, M10)
  let prefillOut = null;
  let llmNotes = []; // LLM 이 채운 값의 인용 경고. 사용자가 그 칸을 고치면 사라진다

  /** 입력값에서 where(검증 결과의 자리)의 지금 값. */
  function valueAt(where) {
    const m = /^(.+)\[(\d+)\]\.c(\d+)$/.exec(where);
    if (m) return ((raw.rows[m[1]] || [])[Number(m[2])] || {})[m[3]] || "";
    return raw.cells[where] ?? raw.fields[where] ?? "";
  }

  function liveNotes() {
    llmNotes = llmNotes.filter((n) => valueAt(n.issue.where) === n.value);
    return llmNotes.map((n) => n.issue);
  }

  async function prefillGo() {
    const text = $("prefill-text").value.trim();
    if (!text) { DF.toast("채울 내용을 적으세요.", "error"); return; }
    const btn = $("prefill-go");
    btn.disabled = true;
    btn.textContent = "채우는 중… (1분쯤 걸릴 수 있습니다)";
    try {
      prefillOut = await api("POST", `/api/documents/${docId}/prefill`, { prompt: text, variant: raw.variant ?? null });
      showPrefill(prefillOut);
    } catch (e) { DF.fail(e); } finally {
      btn.disabled = false;
      btn.textContent = "채우기";
    }
  }

  function showPrefill(out) {
    const warned = new Map(out.issues.map((i) => [i.where, i]));
    const note = $("prefill-note");
    note.replaceChildren(
      h("p", { class: "small" }, `${out.filled.length}칸을 채웠습니다`, out.issues.length ? ` · 확인할 값 ${out.issues.length}건` : "",
        h("span", { class: "muted" }, ` (${out.model}, ${out.elapsed}초)`)),
      out.skipped.length ? h("div", { class: "msg info small" }, `LLM 에 넘기지 않은 칸: ${out.skipped.join(", ")}`) : "");
    const label = (f) => (f.location.kind === "repeat" ? `${f.label} (${f.location.index + 1}행)` : f.label);
    const current = (f) => (f.location.kind === "repeat" ? "" : valueAt(f.where));
    $("prefill-rows").replaceChildren(
      h("thead", {}, h("tr", {}, h("th", {}, "항목"), h("th", {}, "채울 값"), h("th", {}, "지금 값"), h("th", {}, "확인"))),
      h("tbody", {}, ...out.filled.map((f) => {
        const w = warned.get(f.where);
        return h("tr", { class: w ? "warning" : "" },
          h("td", {}, label(f)), h("td", { style: "white-space:pre-wrap" }, f.value),
          h("td", { class: "muted", style: "white-space:pre-wrap" }, current(f)),
          h("td", { class: "small" }, w ? h("span", { class: "badge warning" }, w.message) : ""));
      })));
    $("prefill-all").disabled = $("prefill-empty").disabled = !out.filled.length;
    $("prefill-dialog").showModal();
  }

  function applyPrefill(overwrite) {
    const out = prefillOut;
    const applied = new Set();
    for (const part of ["fields", "cells"]) {
      for (const [k, v] of Object.entries(out.input[part] || {})) {
        if (overwrite || !String(raw[part][k] ?? "").trim()) { raw[part][k] = v; applied.add(k); }
      }
    }
    // 명단이 아닌 반복영역(지출 내역 등): 덮어쓰면 바꾸고, 아니면 적어 둔 행 뒤에 붙인다
    const shift = {};
    for (const [rid, rows] of Object.entries(out.input.rows || {})) {
      const rep = schema.repeats.find((r) => r.id === rid);
      if (!rep) continue;
      let lines = raw.rows[rid] || [];
      lines = overwrite ? [] : lines.filter((line, i) => rowFilled(rep, line, i));
      shift[rid] = lines.length;
      for (const row of rows) lines.push({ ...newRow(rep, lines.length), ...row });
      raw.rows[rid] = lines;
    }
    const fresh = [];
    for (const i of out.issues) {
      const loc = i.location;
      if (loc.kind === "repeat") {
        const index = shift[loc.id] + loc.index;
        const where = `${loc.id}[${index}].c${loc.col}`;
        fresh.push({ ...i, where, location: { ...loc, index, row: loc.row - loc.index + index } });
      } else if (applied.has(loc.id)) fresh.push(i);
    }
    llmNotes = [...llmNotes.filter((n) => !fresh.some((x) => x.where === n.issue.where)),
      ...fresh.map((issue) => ({ issue, value: valueAt(issue.where) }))];
    $("prefill-dialog").close();
    redraw(); changed(); check();
    const rowCount = Object.values(out.input.rows || {}).reduce((n, r) => n + r.length, 0);
    DF.toast(`${applied.size}칸${rowCount ? `과 ${rowCount}행` : ""}을 넣었습니다.${fresh.length ? ` 확인할 값 ${fresh.length}건은 검증 결과에 경고로 남깁니다.` : ""}`);
  }
  $("prefill-go").addEventListener("click", prefillGo);
  $("prefill-all").addEventListener("click", () => {
    if (confirm("이미 적은 칸도 LLM 이 채운 값으로 바꿀까요?")) applyPrefill(true);
  });
  $("prefill-empty").addEventListener("click", () => applyPrefill(false));
  $("prefill-text").addEventListener("input", (ev) => {
    $("prefill-count").textContent = ev.target.value.length ? `${ev.target.value.length.toLocaleString("ko-KR")} / 4,000자` : "";
  });

  // ---------------------------------------------------------------- 본문 초안 (F2-b, DR_01~08)
  let draftWhere = null;
  let lastDraft = null; // 마지막으로 받은 초안 (채택할 때 인용 근거를 같이 적는다)

  function draftButton(where) {
    if (readonly() || !draftTargets.has(where)) return null;
    return h("button", { type: "button", class: "small", style: "margin-top:4px", onclick: () => openDraft(where) }, "초안 쓰기");
  }

  function openDraft(where) {
    draftWhere = where;
    lastDraft = null;
    $("draft-title").textContent = `본문 초안 — ${draftTargets.get(where)}`;
    $("draft-result").classList.add("hidden");
    $("draft-note").replaceChildren();
    $("draft-text").value = "";
    for (const id of ["draft-retry", "draft-use"]) $(id).classList.add("hidden");
    $("draft-go").classList.remove("hidden");
    $("draft-go").textContent = String(valueAt(where)).trim() ? "지금 글을 고쳐 쓰기" : "초안 만들기";
    $("draft-dialog").showModal();
  }

  async function draftGo(retry) {
    const buttons = ["draft-go", "draft-retry", "draft-use"].map($);
    buttons.forEach((b) => { b.disabled = true; });
    const busy = retry ? $("draft-retry") : $("draft-go");
    const label = busy.textContent;
    busy.textContent = "쓰는 중… (1분쯤 걸릴 수 있습니다)";
    try {
      const out = await api("POST", `/api/documents/${docId}/draft`,
        { target: draftWhere, input: payload(), instruction: $("draft-instruction").value, retry });
      lastDraft = out;
      $("draft-text").value = out.text;
      $("draft-result").classList.remove("hidden");
      $("draft-note").replaceChildren(
        ...out.issues.map((i) => h("div", { class: "msg warn small", style: "margin-top:8px" }, `${i.message} → ${i.action}`)),
        h("p", { class: "small muted" }, out.cited.length ? `인용한 입력값: ${out.cited.map((c) => c.label).join(", ")}` : "인용한 입력값이 없습니다.",
          ` (${out.model}, ${out.elapsed}초)`));
      $("draft-go").classList.add("hidden");
      $("draft-retry").classList.remove("hidden");
      $("draft-use").classList.remove("hidden");
    } catch (e) { DF.fail(e); } finally {
      buttons.forEach((b) => { b.disabled = false; });
      busy.textContent = label;
    }
  }

  function useDraft() {
    const text = $("draft-text").value.trim();
    if (!text) { DF.toast("글이 비었습니다.", "error"); return; }
    const part = fieldsMeta.some((f) => f.name === draftWhere) ? "fields" : "cells";
    raw[part][draftWhere] = text;
    raw._drafted[draftWhere] = lastDraft ? lastDraft.basis : "";
    $("draft-dialog").close();
    redraw(); changed(); check();
    DF.toast("초안을 넣었습니다. 입력값과 숫자가 어긋나면 검증 결과에 경고가 나옵니다.");
  }
  $("draft-go").addEventListener("click", () => draftGo(false));
  $("draft-retry").addEventListener("click", () => draftGo(true));
  $("draft-use").addEventListener("click", useDraft);

  // ---------------------------------------------------------------- 정성 판정 (ER_05·ER_06)
  let reviewed = null; // { snapshot: 판정한 입력값, issues }

  /** 판정한 입력값이 그대로일 때만 판정 경고를 보인다. 고치면 다시 판정해야 한다. */
  function reviewIssues() {
    if (!reviewed) return [];
    if (JSON.stringify(payload()) === reviewed.snapshot) return reviewed.issues;
    reviewed = null;
    $("review-state").textContent = "입력이 바뀌었습니다. 필요하면 다시 판정하세요";
    return [];
  }

  async function reviewGo() {
    const btn = $("btn-review");
    btn.disabled = true;
    btn.textContent = "판정 중… (1분쯤 걸릴 수 있습니다)";
    try {
      const input = payload();
      const out = await api("POST", `/api/documents/${docId}/review`, { input });
      reviewed = { snapshot: JSON.stringify(input), issues: out.issues };
      const count = (v) => out.results.filter((r) => r.verdict === v).length;
      $("review-state").textContent = `적합 ${count("pass")} · 부적합 ${count("fail")} · 판단 불가 ${count("unknown")}`;
      showReview(out);
      await check();
    } catch (e) { DF.fail(e); } finally {
      btn.disabled = false;
      btn.textContent = "정성 판정";
    }
  }

  function showReview(out) {
    const cls = { pass: "completed", fail: "error", unknown: "warning" };
    $("review-rows").replaceChildren(
      h("thead", {}, h("tr", {}, h("th", {}, "물음"), h("th", {}, "판정"), h("th", {}, "이유"), h("th", {}, "근거"))),
      h("tbody", {}, ...out.results.map((r) => h("tr", {},
        h("td", {}, r.question),
        h("td", {}, h("span", { class: `badge ${cls[r.verdict]}` }, r.verdict_name)),
        h("td", { class: "small" }, r.reason),
        h("td", { class: "small" }, !r.evidence ? h("span", { class: "muted" }, "없음")
          : r.verified ? h("button", { type: "button", class: "small link", onclick: () => { $("review-dialog").close(); focusWhere(r.where); } }, `“${r.evidence}”`)
            : h("span", { class: "muted", title: "문서에서 이 문장을 찾지 못했습니다" }, `“${r.evidence}” (문서에 없음)`))))));
    $("review-meta").textContent = `${out.model}, ${out.elapsed}초`;
    $("review-dialog").showModal();
  }
  $("btn-review").addEventListener("click", reviewGo);

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
    $("prefill-card").classList.toggle("hidden", readonly());
    $("review-bar").classList.toggle("hidden", readonly() || !checklist);
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
    const notes = [...liveNotes(), ...reviewIssues()];
    const warns = r.issues.length + notes.length;
    const note = errors ? `오류 ${errors}건을 고쳐야 문서를 만들 수 있습니다.` : warns ? `오류는 없고 경고 ${warns}건이 있습니다. 확인 후 만들 수 있습니다.` : "검증을 통과했습니다. 문서를 만들 수 있습니다.";
    showIssues([...r.issues, ...notes], note);
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

  // ---------------------------------------------------------------- 연결 문서 (VA_11)
  async function loadLinks() {
    const links = await api("GET", `/api/documents/${docId}/links`);
    $("links-card").classList.toggle("hidden", !links.length);
    $("links").replaceChildren(...links.map((l) => {
      const other = `${l.other_form.form_no} ${l.other_form.title}`;
      const sel = h("select", { "aria-label": `${l.name} 연결 문서`, disabled: readonly(), onchange: async (ev) => {
        try {
          const id = ev.target.value ? Number(ev.target.value) : null;
          await api("PUT", `/api/documents/${docId}/links/${l.rule_id}`, { document_id: id });
          DF.toast(id ? "문서를 이었습니다." : "연결을 끊었습니다.");
          scheduleCheck();
        } catch (e) { DF.fail(e); loadLinks().catch(DF.fail); }
      } },
      h("option", { value: "" }, "(잇지 않음)"),
      ...l.candidates.map((d) => h("option", { value: d.id, selected: l.linked && l.linked.id === d.id },
        `${d.title} · ${DF.STATUS[d.status] || d.status} · ${DF.fmtTime(d.updated_at)}`)));
      return h("div", { style: "margin-bottom:10px" },
        h("div", { class: "small" }, h("b", {}, l.name), ` — ${other}`),
        l.candidates.length ? sel : h("div", { class: "muted small" }, `내 문서함에 ${other} 문서가 없습니다.`));
    }));
  }

  // ---------------------------------------------------------------- 시작
  async function load() {
    const [d, f] = await Promise.all([api("GET", `/api/documents/${docId}`), api("GET", `/api/documents/${docId}/form`)]);
    doc = d;
    schema = f.schema;
    layout = f.layout;
    fieldsMeta = f.fields;
    draftTargets = new Map((f.draft_targets || []).map((t) => [t.where, t.label]));
    checklist = f.checklist || 0;
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
    loadLinks().catch(DF.fail);
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
