// DocFlow 화면 공통: API 호출, 세션 타이머(AU_04), 알림, 작은 DOM 도우미.
"use strict";

const DF = (window.DF = window.DF || {});

class ApiError extends Error {
  constructor(status, body) {
    super((body && body.detail) || `요청 실패 (${status})`);
    this.status = status;
    this.body = body || {};
  }
}
DF.ApiError = ApiError;

/** /api 호출. 세션이 끊겼으면 로그인 쪽으로 보낸다. */
DF.api = async function (method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (res.ok) return data;
  if (res.status === 401 && !url.startsWith("/api/auth/login")) {
    DF.toLogin((data && data.reason) || "expired");
    throw new ApiError(res.status, data);
  }
  if (res.status === 403 && data && data.reason === "password_change_required") {
    location.href = "/password?required=1";
    throw new ApiError(res.status, data);
  }
  throw new ApiError(res.status, data);
};

/** 파일 올리기 (multipart). 응답 처리는 DF.api 와 같다. */
DF.upload = async function (url, formData) {
  const res = await fetch(url, { method: "POST", body: formData });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (res.ok) return data;
  if (res.status === 401) DF.toLogin((data && data.reason) || "expired");
  throw new ApiError(res.status, data);
};

DF.toLogin = function (reason) {
  const next = encodeURIComponent(location.pathname + location.search);
  location.href = `/login?next=${next}&reason=${encodeURIComponent(reason || "")}`;
};

/** 요소 만들기: h("td", {class: "x", onclick: f}, "글자", 자식…) */
DF.h = function (tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
};

DF.toast = function (text, kind) {
  let box = document.getElementById("toasts");
  if (!box) {
    box = DF.h("div", { id: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(box);
  }
  const t = DF.h("div", { class: `toast ${kind || ""}` }, text);
  box.append(t);
  setTimeout(() => t.remove(), kind === "error" ? 6000 : 3000);
};

DF.fail = function (err) {
  if (err instanceof ApiError && err.status === 401) return;
  DF.toast(err.message || String(err), "error");
  console.error(err);
};

/** 서버 시각(UTC, 시간대 표시 없음)을 이 PC 의 시각으로. 시간대가 없으면 UTC 로 읽는다. */
DF.fmtTime = function (iso) {
  if (!iso) return "";
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}.${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

DF.STATUS = { draft: "작성 중", completed: "완료" };
DF.qs = (name) => new URLSearchParams(location.search).get(name);

// ---------------------------------------------------------------- 세션 타이머 (AU_04)
// 우측 상단에 남은 시간과 연장 버튼. 입력 중에 만료되면 경고 후 5분 자동 연장(한 번).
DF.session = {
  expiresAt: 0,
  graceUsed: false,
  lastInput: 0,
  warned: false,
  apply(s) {
    this.expiresAt = Date.now() + s.remaining_seconds * 1000;
    this.graceUsed = s.grace_used;
    this.warned = false;
  },
  async load() {
    this.apply(await DF.api("GET", "/api/auth/me"));
  },
  async extend() {
    try {
      this.apply(await DF.api("POST", "/api/auth/extend"));
      DF.toast("세션을 30분 연장했습니다.");
    } catch (e) { DF.fail(e); }
  },
  async tick() {
    const el = document.getElementById("timer");
    if (!el || !this.expiresAt) return;
    const left = Math.max(0, Math.round((this.expiresAt - Date.now()) / 1000));
    el.textContent = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
    el.classList.toggle("low", left <= 300);
    if (left > 0 || this.warned) return;
    this.warned = true;
    const typing = Date.now() - this.lastInput < 5 * 60 * 1000;
    if (typing && !this.graceUsed) {
      try {
        this.apply(await DF.api("POST", "/api/auth/grace"));
        alert("세션이 만료되어 입력 중인 내용을 지키려고 5분 연장했습니다.\n계속 작성하려면 우측 상단의 [연장]을 누르세요.");
        return;
      } catch { /* 아래에서 로그인으로 */ }
    }
    DF.toLogin("expired");
  },
};

document.addEventListener("input", () => { DF.session.lastInput = Date.now(); }, true);

document.addEventListener("DOMContentLoaded", () => {
  const timer = document.getElementById("timer");
  if (timer) {
    DF.session.load().catch(DF.fail);
    setInterval(() => DF.session.tick(), 1000);
    document.getElementById("extend").addEventListener("click", () => DF.session.extend());
  }
  const out = document.getElementById("logout");
  if (out) {
    out.addEventListener("click", async () => {
      try { await DF.api("POST", "/api/auth/logout"); } catch { /* 이미 끊긴 세션 */ }
      location.href = "/login";
    });
  }
});
