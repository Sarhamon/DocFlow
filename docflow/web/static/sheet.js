// 서식 표를 서식과 같은 모양(병합 포함)으로 그린다. 미리보기와 입력 화면이 같이 쓴다.
"use strict";

(() => {
  const { h } = DF;

  /** 슬롯·수식·반복영역을 표 번호별로 묶는다. */
  DF.indexSchema = function (schema) {
    const by = new Map();
    const get = (t) => {
      if (!by.has(t)) by.set(t, { slots: new Map(), formulas: new Map(), repeats: [] });
      return by.get(t);
    };
    for (const s of schema.slots) get(s.table).slots.set(`${s.row},${s.col}`, s);
    for (const f of schema.formulas) get(f.table).formulas.set(`${f.row},${f.col}`, f);
    for (const r of schema.repeats) get(r.table).repeats.push(r);
    return { get, has: (t) => by.has(t) };
  };

  /** 작성자가 고르는 변형. 묶음 파일에서 떼어 낸 서식(schema.variant)은 이미 정해져 있다. */
  DF.choices = (schema) => (schema.variant === null || schema.variant === undefined ? schema.variants : []);

  /** 표가 고른 변형에 들어가는가. 변형이 없거나 어느 변형에도 속하지 않으면 늘 보인다. */
  DF.inVariant = function (schema, table, variant) {
    if (!schema.variants.length || variant === null || variant === undefined) return true;
    const owner = schema.variants.find((v) => v.first_table <= table && table <= v.last_table);
    return !owner || owner.index === variant;
  };

  /**
   * 표 하나를 그린다.
   *   cell(c)   -> 칸을 대신 그릴 {node, cls} 또는 null(서식 글자 그대로)
   *   skipRow(r) -> true 면 그 행을 그리지 않는다
   *   rowsAt(r)  -> 그 행 자리에 끼워 넣을 <tr> 목록 (반복영역)
   */
  DF.renderTable = function (table, { cell, skipRow, rowsAt } = {}) {
    const rows = new Map();
    for (const c of table.cells) {
      if (!rows.has(c.row)) rows.set(c.row, []);
      rows.get(c.row).push(c);
    }
    const tbody = h("tbody");
    for (let r = 0; r < table.rows; r++) {
      if (rowsAt) for (const tr of rowsAt(r) || []) tbody.append(tr);
      if (skipRow && skipRow(r)) continue;
      const tr = h("tr");
      for (const c of (rows.get(r) || []).sort((a, b) => a.col - b.col)) {
        const span = { rowspan: c.row_span > 1 ? spanWithin(c, skipRow) : null, colspan: c.col_span > 1 ? c.col_span : null };
        const custom = cell ? cell(c) : null;
        if (custom) tr.append(h("td", { ...span, class: custom.cls || "" }, custom.node));
        else tr.append(h("td", { ...span, class: c.text.trim() ? "label" : "" }, c.text));
      }
      tbody.append(tr);
    }
    if (rowsAt) for (const tr of rowsAt(table.rows) || []) tbody.append(tr);
    return h("table", { class: "form" }, tbody);
  };

  // 건너뛰는 행(반복영역 데이터 행)에 걸친 병합은 그만큼 줄인다
  function spanWithin(c, skipRow) {
    if (!skipRow) return c.row_span;
    let n = 0;
    for (let r = c.row; r < c.row + c.row_span; r++) if (!skipRow(r)) n++;
    return n > 1 ? n : null;
  }
})();
