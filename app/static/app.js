"use strict";

/* 束流整形磁铁垫片校正复核 —— 前端逻辑。
 *
 * 精确性约定：所有整数在浏览器中始终以十进制字符串保存与展示，
 * 绝不经过 Number 转换，因此超过 2^53 的系数与目标不会丢失精度。
 *
 * 竞态约定：generation 为复核草稿的单调递增世代号。发起复核、编辑草稿、
 * 取消复核都会使其递增；只有世代号仍为当前值的响应才允许渲染，
 * 过期响应一律丢弃，绝不覆盖当前草稿的状态或结论。
 *
 * 行程受限审计复用同一套思路但使用独立世代号 auditGeneration：修改边界
 * 或取消审计只令在途审计过期（不影响仍有效的复核结论），而修改草稿、
 * 重新发起复核则同时令复核与审计在途请求过期。
 */

const $ = (id) => document.getElementById(id);

const draftFields = {
  variables: $("variables"),
  matrix: $("matrix"),
  target: $("target"),
};
const runButton = $("run");
const cancelButton = $("cancel");
const statusLine = $("status");
const resultPanel = $("result-panel");
const resultBox = $("result");

let generation = 0; // 草稿/复核请求世代号
let inflight = null; // 在途复核请求的 AbortController
let auditGeneration = 0; // 行程审计世代号
let auditInflight = null; // 在途审计请求的 AbortController

const INT_PATTERN = /^[+-]?\d+$/;

const EXAMPLE_SOLVABLE = {
  variables: "K1, K2, K3",
  matrix: "9007199254740993 1 0\n1 3 1\n0 2 4",
  target: "18014398509481989, 12, 10",
};

const EXAMPLE_UNSOLVABLE = {
  variables: "D1, D2",
  matrix: "2 0\n0 4",
  target: "9007199254740993, 8",
};

// ---------------------------------------------------------------- 草稿解析

function splitTokens(text) {
  return text.split(/[\s,，]+/).filter((token) => token.length > 0);
}

function parseIntegerToken(token, where) {
  if (!INT_PATTERN.test(token)) {
    throw new Error(
      `${where}：“${token}” 不是十进制整数。仅接受整数字符串，绝不进行浮点换算。`
    );
  }
  let text = token.replace(/^\+/, "");
  const negative = text.startsWith("-");
  let digits = negative ? text.slice(1) : text;
  digits = digits.replace(/^0+(?=\d)/, "");
  return (negative ? "-" : "") + digits;
}

function readDraft() {
  const variables = splitTokens(draftFields.variables.value);
  if (variables.length === 0) {
    throw new Error("请填写变量标识。");
  }
  if (new Set(variables).size !== variables.length) {
    throw new Error("变量标识存在重复。");
  }
  const rows = draftFields.matrix.value
    .split(/\n+/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (rows.length === 0) {
    throw new Error("请填写约束矩阵。");
  }
  const matrix = rows.map((line, i) => {
    const coeffs = splitTokens(line).map((token) =>
      parseIntegerToken(token, `矩阵第 ${i + 1} 行`)
    );
    if (coeffs.length !== variables.length) {
      throw new Error(
        `矩阵第 ${i + 1} 行有 ${coeffs.length} 个系数，与变量数 ${variables.length} 不一致。`
      );
    }
    return coeffs;
  });
  const target = splitTokens(draftFields.target.value).map((token) =>
    parseIntegerToken(token, "目标向量")
  );
  if (target.length !== matrix.length) {
    throw new Error(
      `目标向量长度 ${target.length} 与约束条数 ${matrix.length} 不一致。`
    );
  }
  return { variables, matrix, target };
}

// ---------------------------------------------------------------- 状态与竞态

function setStatus(message, kind) {
  statusLine.textContent = message;
  statusLine.dataset.kind = kind || "";
}

function markResultsStale() {
  if (!resultPanel.hidden && resultPanel.dataset.state === "fresh") {
    resultPanel.dataset.state = "stale";
    resultPanel.classList.add("stale");
  }
}

function invalidateDraft() {
  generation += 1; // 使任何在途复核响应立即过期
  auditGeneration += 1; // 草稿变了，在途审计（基于旧矩阵/目标）同样作废
  if (inflight) {
    inflight.abort();
    inflight = null;
  }
  if (auditInflight) {
    auditInflight.abort();
    auditInflight = null;
  }
  cancelButton.disabled = true;
  markResultsStale();
  setStatus("草稿已修改：先前计算即使返回也将被丢弃，不会覆盖当前草稿。", "warn");
}

Object.values(draftFields).forEach((field) =>
  field.addEventListener("input", invalidateDraft)
);

async function runReview() {
  let payload;
  try {
    payload = readDraft();
  } catch (error) {
    setStatus(error.message, "error");
    return;
  }
  const myGeneration = ++generation;
  auditGeneration += 1; // 新复核取代旧结论时，在途审计同样作废
  if (inflight) {
    inflight.abort();
  }
  if (auditInflight) {
    auditInflight.abort();
    auditInflight = null;
  }
  const controller = new AbortController();
  inflight = controller;
  reviewPayload = payload;
  cancelButton.disabled = false;
  setStatus("复核计算中……", "busy");
  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (myGeneration !== generation) {
      return; // 草稿已变更或已取消：丢弃过期响应
    }
    if (!response.ok || !data || data.ok !== true) {
      const message = data && data.error ? data.error : `HTTP ${response.status}`;
      setStatus(`复核失败：${message}`, "error");
      return;
    }
    renderResult(data);
    resultPanel.hidden = false;
    resultPanel.dataset.state = "fresh";
    resultPanel.classList.remove("stale");
    setStatus("复核完成。", "ok");
  } catch (error) {
    if (error && error.name === "AbortError") {
      return; // 已被取消或被更新的请求取代
    }
    if (myGeneration !== generation) {
      return;
    }
    setStatus(`复核请求失败：${error.message || error}`, "error");
  } finally {
    if (myGeneration === generation) {
      inflight = null;
      cancelButton.disabled = true;
    }
  }
}

function cancelReview() {
  generation += 1;
  auditGeneration += 1;
  if (inflight) {
    inflight.abort();
    inflight = null;
  }
  if (auditInflight) {
    auditInflight.abort();
    auditInflight = null;
  }
  cancelButton.disabled = true;
  setStatus("已取消：先前计算若返回将被丢弃，不会覆盖当前草稿的状态或结论。", "warn");
}

runButton.addEventListener("click", runReview);
cancelButton.addEventListener("click", cancelReview);

// ---------------------------------------------------------------- 行程审计

let reviewPayload = null; // 最近一次渲染的复核请求（审计需重发矩阵与目标）
let auditSection = null; // 当前可解结论中的审计区节点

function invalidateAudit(message) {
  auditGeneration += 1;
  if (auditInflight) {
    auditInflight.abort();
    auditInflight = null;
  }
  if (auditSection) {
    auditSection.auditCancel.disabled = true;
    auditSection.auditRun.disabled = false;
    if (auditSection.auditResult.dataset.state === "fresh") {
      auditSection.auditResult.dataset.state = "stale";
      auditSection.auditResult.classList.add("stale");
    }
  }
  if (message) {
    setAuditStatus(message, "warn");
  }
}

function readBounds() {
  const bounds = [];
  reviewPayload.variables.forEach((name, i) => {
    const row = auditSection.rows[i];
    const minText = row.min.value.trim();
    const maxText = row.max.value.trim();
    if (minText.length === 0 || maxText.length === 0) {
      throw new Error(`变量 ${name} 的最小/最大垫片数尚未填写完整。`);
    }
    if (!INT_PATTERN.test(minText)) {
      throw new Error(`变量 ${name} 的最小垫片数 “${minText}” 不是十进制整数。`);
    }
    if (!INT_PATTERN.test(maxText)) {
      throw new Error(`变量 ${name} 的最大垫片数 “${maxText}” 不是十进制整数。`);
    }
    const minValue = parseIntegerToken(minText, `变量 ${name} 的最小垫片数`);
    const maxValue = parseIntegerToken(maxText, `变量 ${name} 的最大垫片数`);
    if (compareIntegerText(minValue, maxValue) > 0) {
      throw new Error(`变量 ${name} 的最小垫片数大于最大垫片数。`);
    }
    bounds.push({ variable: name, min: minValue, max: maxValue });
  });
  return bounds;
}

// 任意长度十进制整数文本的大小比较（绝不经过 Number），返回 -1/0/1。
function compareIntegerText(x, y) {
  const negX = x.startsWith("-");
  const negY = y.startsWith("-");
  if (negX !== negY) {
    return negX ? -1 : 1;
  }
  const ax = negX ? x.slice(1) : x;
  const ay = negY ? y.slice(1) : y;
  let order = 0;
  if (ax.length !== ay.length) {
    order = ax.length < ay.length ? -1 : 1;
  } else if (ax < ay) {
    order = -1;
  } else if (ax > ay) {
    order = 1;
  }
  return negX ? -order : order;
}

function setAuditStatus(message, kind) {
  if (!auditSection) {
    return;
  }
  auditSection.status.textContent = message;
  auditSection.status.dataset.kind = kind || "";
}

async function runAudit() {
  if (!auditSection || !reviewPayload) {
    return;
  }
  let bounds;
  try {
    bounds = readBounds();
  } catch (error) {
    setAuditStatus(error.message, "error");
    return;
  }
  const myGeneration = ++auditGeneration;
  if (auditInflight) {
    auditInflight.abort();
  }
  const controller = new AbortController();
  auditInflight = controller;
  auditSection.auditRun.disabled = true;
  auditSection.auditCancel.disabled = false;
  setAuditStatus("行程受限审计计算中：正在解格上枚举有界整数方案……", "busy");
  const payload = {
    variables: reviewPayload.variables,
    matrix: reviewPayload.matrix,
    target: reviewPayload.target,
    bounds,
  };
  try {
    const response = await fetch("/api/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (myGeneration !== auditGeneration) {
      return; // 边界已修改、草稿已变更或审计已取消：丢弃过期响应
    }
    if (!response.ok || !data || data.ok !== true) {
      const message = data && data.error ? data.error : `HTTP ${response.status}`;
      setAuditStatus(`边界格式非法或请求被拒绝：${message}`, "error");
      return;
    }
    renderAuditResult(data);
    setAuditStatus("行程受限审计完成。", "ok");
  } catch (error) {
    if (error && error.name === "AbortError") {
      return;
    }
    if (myGeneration !== auditGeneration) {
      return;
    }
    setAuditStatus(`行程审计请求失败：${error.message || error}`, "error");
  } finally {
    if (myGeneration === auditGeneration) {
      auditInflight = null;
      if (auditSection) {
        auditSection.auditRun.disabled = false;
        auditSection.auditCancel.disabled = true;
      }
    }
  }
}

function cancelAudit() {
  invalidateAudit("已取消行程审计：较早返回若到达也将被丢弃，不会覆盖当前草稿与结论。");
}

function loadExample(example) {
  draftFields.variables.value = example.variables;
  draftFields.matrix.value = example.matrix;
  draftFields.target.value = example.target;
  invalidateDraft();
  setStatus("示例已载入，可发起复核。", "");
}

$("load-solvable").addEventListener("click", () => loadExample(EXAMPLE_SOLVABLE));
$("load-unsolvable").addEventListener("click", () => loadExample(EXAMPLE_UNSOLVABLE));

// ---------------------------------------------------------------- 结果渲染

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text; // 精确整数文本，绝不经过 Number
  }
  return node;
}

function renderResult(data) {
  resultBox.replaceChildren();
  auditSection = null; // 旧结论中的审计区随其 DOM 一起失效
  if (data.solvable) {
    renderSolution(data);
  } else {
    renderObstruction(data);
  }
  resultBox.appendChild(renderSmithSummary(data.smith));
}

function renderSolution(data) {
  resultBox.appendChild(
    el("p", "verdict ok", "结论：可解 —— 以下为精确整数校正量（最小垫片单位的整数倍）。")
  );
  const list = el("ul", "solution-list");
  data.variables.forEach((name, i) => {
    list.appendChild(el("li", "num", `${name} = ${data.solution[i]}`));
  });
  resultBox.appendChild(list);

  resultBox.appendChild(
    el("h3", null, "约束复核（点选任一约束，查看各项乘积、左侧和及其目标值）")
  );
  const listBox = el("div", "constraint-list");
  const detail = el("div", "constraint-detail");
  data.constraints.forEach((constraint, idx) => {
    const button = el(
      "button",
      "constraint-item num",
      `约束 ${idx + 1}：左侧和 ${constraint.sum} ＝ 目标 ${constraint.target} ${
        constraint.satisfied ? "✓" : "✗"
      }`
    );
    button.type = "button";
    button.addEventListener("click", () => {
      listBox
        .querySelectorAll(".constraint-item")
        .forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      renderConstraintDetail(detail, constraint);
    });
    listBox.appendChild(button);
  });
  resultBox.appendChild(listBox);
  resultBox.appendChild(detail);
  const first = listBox.querySelector(".constraint-item");
  if (first) {
    first.click();
  }

  if (data.homogeneousBasis && data.homogeneousBasis.length > 0) {
    const details = el("details", "homogeneous");
    details.appendChild(
      el(
        "summary",
        null,
        `自由校正方向（${data.homogeneousBasis.length} 个）：叠加其任意整数倍不改变任何左侧和`
      )
    );
    const table = el("table", "num");
    const head = el("tr");
    head.appendChild(el("th", null, "方向"));
    data.variables.forEach((name) => head.appendChild(el("th", null, name)));
    table.appendChild(head);
    data.homogeneousBasis.forEach((vector, k) => {
      const row = el("tr");
      row.appendChild(el("td", null, `方向 ${k + 1}`));
      vector.forEach((value) => row.appendChild(el("td", null, value)));
      table.appendChild(row);
    });
    details.appendChild(table);
    resultBox.appendChild(details);
  }

  resultBox.appendChild(renderAuditSection(data));
}

function renderAuditSection(data) {
  const section = el("section", "audit");
  section.appendChild(el("h3", null, "行程受限审计（机械装配边界下的有界整数方案）"));
  section.appendChild(
    el(
      "p",
      "hint",
      "为每个机械校正项填写允许的最小与最大垫片数（整数，可超过 2⁵³，以精确文本处理），"
        + "再发起审计：系统在“特解 + 齐次解格”上枚举全部有界整数方案，"
        + "返回相对当前精确校正量总绝对偏移最小的一组调整；总偏移相同时，"
        + "按变量标识顺序的偏移向量稳定裁决。绝不将连续解取整或沿单一自由方向贪心试探。"
    )
  );

  const table = el("table", "num audit-bounds");
  const head = el("tr");
  ["变量", "当前精确校正量", "最小垫片数", "最大垫片数"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  const rows = [];
  data.variables.forEach((name, i) => {
    const row = el("tr");
    row.appendChild(el("td", null, name));
    row.appendChild(el("td", null, data.solution[i]));
    const minCell = el("td");
    const maxCell = el("td");
    const minInput = document.createElement("input");
    minInput.type = "text";
    minInput.spellcheck = false;
    minInput.autocomplete = "off";
    minInput.className = "bound-input";
    minInput.setAttribute("aria-label", `${name} 最小垫片数`);
    const maxInput = document.createElement("input");
    maxInput.type = "text";
    maxInput.spellcheck = false;
    maxInput.autocomplete = "off";
    maxInput.className = "bound-input";
    maxInput.setAttribute("aria-label", `${name} 最大垫片数`);
    minCell.appendChild(minInput);
    maxCell.appendChild(maxInput);
    row.appendChild(minCell);
    row.appendChild(maxCell);
    table.appendChild(row);
    rows.push({ min: minInput, max: maxInput });
  });
  section.appendChild(table);

  const actions = el("div", "actions audit-actions");
  const prefill = el("button", "ghost", "以当前校正量预填边界");
  prefill.type = "button";
  prefill.addEventListener("click", () => {
    rows.forEach((row, i) => {
      row.min.value = data.solution[i];
      row.max.value = data.solution[i];
    });
    invalidateAudit("已以当前精确校正量预填边界，可按需放宽后发起审计。");
  });
  const auditRun = el("button", "primary", "发起行程受限审计");
  auditRun.type = "button";
  const auditCancel = el("button", null, "取消审计");
  auditCancel.type = "button";
  auditCancel.disabled = true;
  actions.appendChild(auditRun);
  actions.appendChild(auditCancel);
  actions.appendChild(prefill);
  section.appendChild(actions);

  const status = el("p", "audit-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  section.appendChild(status);
  const auditResult = el("div", "audit-result");
  section.appendChild(auditResult);

  auditRun.addEventListener("click", runAudit);
  auditCancel.addEventListener("click", cancelAudit);
  rows.forEach((row) => {
    row.min.addEventListener("input", () =>
      invalidateAudit("边界已修改：较早的审计返回将被丢弃，不会覆盖当前边界与结论。")
    );
    row.max.addEventListener("input", () =>
      invalidateAudit("边界已修改：较早的审计返回将被丢弃，不会覆盖当前边界与结论。")
    );
  });

  auditSection = {
    section,
    rows,
    auditRun,
    auditCancel,
    status,
    auditResult,
  };
  return section;
}

function renderAuditResult(data) {
  const box = auditSection.auditResult;
  box.replaceChildren();
  box.dataset.state = "fresh";
  box.classList.remove("stale");
  const audit = data.audit;
  const boundsByName = new Map(audit.bounds.map((entry) => [entry.variable, entry]));

  if (audit.status === "optimal") {
    box.appendChild(
      el(
        "p",
        "verdict ok",
        `审计结论：装配边界内存在整数方案；以下调整的总绝对偏移最小，为 ${audit.totalAbsDeviation}（垫片单位）。`
      )
    );
    const table = el("table", "num");
    const head = el("tr");
    ["变量", "边界 [min, max]", "当前校正量", "调整后校正量", "偏移（调整 − 当前）"].forEach(
      (title) => head.appendChild(el("th", null, title))
    );
    table.appendChild(head);
    data.variables.forEach((name, i) => {
      const bound = boundsByName.get(name);
      const row = el("tr");
      row.appendChild(el("td", null, name));
      row.appendChild(el("td", null, `[${bound.min}, ${bound.max}]`));
      row.appendChild(el("td", null, data.solution[i]));
      row.appendChild(el("td", null, audit.adjustment[i]));
      const deviation = audit.deviation[i];
      const shown = deviation.startsWith("-") ? deviation : (deviation === "0" ? "0" : `+${deviation}`);
      row.appendChild(el("td", null, shown));
      table.appendChild(row);
    });
    const totalRow = el("tr");
    totalRow.appendChild(el("th", null, "总绝对偏移"));
    totalRow.appendChild(el("td", null, "—"));
    totalRow.appendChild(el("td", null, "—"));
    totalRow.appendChild(el("td", null, "—"));
    totalRow.appendChild(el("th", null, audit.totalAbsDeviation));
    table.appendChild(totalRow);
    box.appendChild(table);

    box.appendChild(
      el(
        "p",
        "explanation",
        "下表为调整后方案对原始耦合位移方程的逐条精确复算；原始方程与全部边界同时成立。"
      )
    );
    box.appendChild(
      el("h4", null, "调整后约束精确复算（点选任一约束查看各项乘积、左侧和及其目标值）")
    );
    const listBox = el("div", "constraint-list");
    const detail = el("div", "constraint-detail");
    audit.constraints.forEach((constraint, idx) => {
      const button = el(
        "button",
        "constraint-item num",
        `约束 ${idx + 1}：左侧和 ${constraint.sum} ＝ 目标 ${constraint.target} ${
          constraint.satisfied ? "✓" : "✗"
        }`
      );
      button.type = "button";
      button.addEventListener("click", () => {
        listBox
          .querySelectorAll(".constraint-item")
          .forEach((node) => node.classList.remove("active"));
        button.classList.add("active");
        renderConstraintDetail(detail, constraint);
      });
      listBox.appendChild(button);
    });
    box.appendChild(listBox);
    box.appendChild(detail);
    const first = listBox.querySelector(".constraint-item");
    if (first) {
      first.click();
    }
    return;
  }

  if (audit.status === "infeasible") {
    box.appendChild(
      el(
        "p",
        "verdict bad",
        "审计结论：原始方程可解，但在给定装配边界内不存在任何整数校正方案。"
      )
    );
    box.appendChild(
      el(
        "p",
        "explanation",
        "系统已在“特解 + 齐次解格”的全部有界整数坐标上完成精确枚举（区间传播与分支定界"
          + "证明边界盒与整数解格不相交），并非由连续解取整失败得出；上方可解复核结论"
          + "（特解、齐次方向、每条原始约束的乘积复算）原样保留，可继续核对或放宽边界后重审。"
      )
    );
  } else {
    // status === "unsolvable": defensive — audit is only offered on a
    // solvable conclusion, so this indicates the re-sent equations differ.
    box.appendChild(
      el(
        "p",
        "verdict bad",
        "审计结论：方程本身无整数解，无从讨论装配边界；请先查看上方复核给出的规范除尽障碍。"
      )
    );
  }
  const table = el("table", "num");
  const head = el("tr");
  ["变量", "边界 [min, max]"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  data.variables.forEach((name) => {
    const bound = boundsByName.get(name);
    const row = el("tr");
    row.appendChild(el("td", null, name));
    row.appendChild(el("td", null, `[${bound.min}, ${bound.max}]`));
    table.appendChild(row);
  });
  box.appendChild(table);
}

function renderConstraintDetail(parent, constraint) {
  parent.replaceChildren();
  const table = el("table", "num");
  const head = el("tr");
  ["变量", "系数", "校正量", "乘积（系数 × 校正量）"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  constraint.terms.forEach((term) => {
    const row = el("tr");
    row.appendChild(el("td", null, term.variable));
    row.appendChild(el("td", null, term.coefficient));
    row.appendChild(el("td", null, term.correction));
    row.appendChild(el("td", null, term.product));
    table.appendChild(row);
  });
  parent.appendChild(table);
  parent.appendChild(el("p", "num", `左侧和 = ${constraint.sum}`));
  parent.appendChild(el("p", "num", `目标值 = ${constraint.target}`));
  parent.appendChild(
    el(
      "p",
      constraint.satisfied ? "verdict ok" : "verdict bad",
      constraint.satisfied ? "左侧和与目标值精确相等 ✓" : "左侧和与目标值不相等 ✗"
    )
  );
}

function renderObstruction(data) {
  const ob = data.obstruction;
  resultBox.appendChild(
    el("p", "verdict bad", "结论：无整数解 —— 存在由可逆整数行变换导出的规范除尽障碍。")
  );
  const explanation =
    ob.type === "non_divisible"
      ? `对约束系统施加可逆整数行变换（Smith 正规形 U·A·V = D）后，第 ${
          ob.row + 1
        } 行的主元为 ${ob.pivot}，而变换后目标为 ${
          ob.transformedTarget
        }，余数 ${ob.remainder} ≠ 0：主元不能整除变换后目标，因此不存在整数校正量。`
      : `对约束系统施加可逆整数行变换（Smith 正规形 U·A·V = D）后，第 ${
          ob.row + 1
        } 行为零行（主元 0），但变换后目标为 ${
          ob.transformedTarget
        } ≠ 0：等式 0 = ${ob.transformedTarget} 不可能成立。`;
  resultBox.appendChild(el("p", "explanation", explanation));

  const facts = el("table", "num facts");
  [
    ["障碍类型", ob.type === "non_divisible" ? "主元除尽失败" : "零行目标非零"],
    ["变换后行号", `第 ${ob.row + 1} 行`],
    ["主元", ob.pivot],
    ["变换后目标", ob.transformedTarget],
    ["余数（变换后目标 mod 主元）", ob.remainder],
  ].forEach(([key, value]) => {
    const row = el("tr");
    row.appendChild(el("th", null, key));
    row.appendChild(el("td", null, value));
    facts.appendChild(row);
  });
  resultBox.appendChild(facts);

  resultBox.appendChild(
    el(
      "h3",
      null,
      `行变换来源：变换后目标 = 第 ${ob.row + 1} 行变换行 U 与原始目标向量的乘积和`
    )
  );
  const table = el("table", "num");
  const head = el("tr");
  ["原约束行", "行变换系数 U", "原目标值", "乘积（U × 原目标）"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  table.appendChild(head);
  ob.uRowTerms.forEach((term) => {
    const row = el("tr");
    row.appendChild(el("td", null, `约束 ${term.row + 1}`));
    row.appendChild(el("td", null, term.coefficient));
    row.appendChild(el("td", null, term.target));
    row.appendChild(el("td", null, term.product));
    table.appendChild(row);
  });
  resultBox.appendChild(table);
  resultBox.appendChild(
    el("p", "num", `各项乘积之和 = ${ob.transformedTarget}（即变换后目标）`)
  );
}

function renderSmithSummary(smith) {
  const details = el("details", "smith");
  details.appendChild(el("summary", null, "Smith 正规形摘要（可逆整数行变换导出）"));
  details.appendChild(el("p", "num", `秩 = ${smith.rank}`));
  details.appendChild(
    el("p", "num", `主元序列（d₁ | d₂ | …）= [${smith.diagonal.join(", ")}]`)
  );
  details.appendChild(
    el("p", "num", `变换后目标向量 U·b = [${smith.transformedTarget.join(", ")}]`)
  );
  return details;
}

// ---------------------------------------------------------------- 初始化

loadExample(EXAMPLE_SOLVABLE);
setStatus("已载入可解示例，可直接发起复核。", "");
