"use strict";

/* 束流整形磁铁垫片校正复核 —— 前端逻辑。
 *
 * 精确性约定：所有整数在浏览器中始终以十进制字符串保存与展示，
 * 绝不经过 Number 转换，因此超过 2^53 的系数与目标不会丢失精度。
 *
 * 竞态约定：generation 为单调递增的世代号。发起复核、编辑草稿、
 * 取消复核都会使其递增；只有世代号仍为当前值的响应才允许渲染，
 * 过期响应一律丢弃，绝不覆盖当前草稿的状态或结论。
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

let generation = 0; // 草稿/请求世代号
let inflight = null; // 在途请求（复核或审计）的 AbortController
let lastPayload = null; // 最近一次成功复核的精确草稿（变量/矩阵/目标）

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
  generation += 1; // 使任何在途响应立即过期
  if (inflight) {
    inflight.abort();
    inflight = null;
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
  if (inflight) {
    inflight.abort();
  }
  lastPayload = payload;
  const controller = new AbortController();
  inflight = controller;
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
  if (inflight) {
    inflight.abort();
    inflight = null;
  }
  cancelButton.disabled = true;
  setStatus("已取消：先前计算若返回将被丢弃，不会覆盖当前草稿的状态或结论。", "warn");
}

runButton.addEventListener("click", runReview);
cancelButton.addEventListener("click", cancelReview);

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

  resultBox.appendChild(buildAuditSection(data));
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

// ------------------------------------------------------------ 行程受限审计

function buildAuditSection(data) {
  const section = el("section", "audit");
  section.appendChild(
    el("h3", null, "行程受限审计（装配边界下的全局最优整数方案）")
  );
  section.appendChild(
    el(
      "p",
      "hint",
      "为每个机械校正项填写最小/最大垫片数（任一可空，留空表示该侧不限）。" +
        "审计从当前精确特解与齐次整数解格出发，在边界整数格点中全局枚举，" +
        "返回相对当前校正量总绝对偏移最小的一组；并列时按变量标识顺序的偏移" +
        "向量字典序裁决，绝不将连续解取整或沿单个自由方向贪心。"
    )
  );

  const grid = el("table", "num audit-grid");
  const head = el("tr");
  ["校正项", "当前精确校正量", "最小垫片数", "最大垫片数"].forEach((title) =>
    head.appendChild(el("th", null, title))
  );
  grid.appendChild(head);
  const minInputs = [];
  const maxInputs = [];
  data.variables.forEach((name, j) => {
    const row = el("tr");
    row.appendChild(el("td", null, name));
    row.appendChild(el("td", null, data.solution[j]));
    const minInput = el("input");
    minInput.type = "text";
    minInput.spellcheck = false;
    minInput.autocomplete = "off";
    minInput.className = "bound-input num";
    minInput.placeholder = "不限";
    const maxInput = el("input");
    maxInput.type = "text";
    maxInput.spellcheck = false;
    maxInput.autocomplete = "off";
    maxInput.className = "bound-input num";
    maxInput.placeholder = "不限";
    const minCell = el("td");
    const maxCell = el("td");
    minCell.appendChild(minInput);
    maxCell.appendChild(maxInput);
    row.appendChild(minCell);
    row.appendChild(maxCell);
    grid.appendChild(row);
    minInputs.push(minInput);
    maxInputs.push(maxInput);
  });
  section.appendChild(grid);

  const actions = el("div", "actions");
  const runAuditButton = el("button", "primary", "发起行程受限审计");
  runAuditButton.type = "button";
  const clearAuditButton = el("button", "ghost", "清空边界 / 取消审计");
  actions.appendChild(runAuditButton);
  actions.appendChild(clearAuditButton);
  section.appendChild(actions);

  const auditStatus = el("p", "audit-status");
  auditStatus.setAttribute("role", "status");
  section.appendChild(auditStatus);
  const auditResult = el("div", "audit-result");
  section.appendChild(auditResult);

  function setAuditStatus(message, kind) {
    auditStatus.textContent = message;
    auditStatus.dataset.kind = kind || "";
  }

  function collectBounds() {
    const bounds = [];
    for (let j = 0; j < data.variables.length; j += 1) {
      const minText = minInputs[j].value.trim();
      const maxText = maxInputs[j].value.trim();
      if (!minText && !maxText) {
        bounds.push(null);
        continue;
      }
      if (minText && !INT_PATTERN.test(minText)) {
        throw new Error(
          `${data.variables[j]} 的最小垫片数“${minText}”不是十进制整数；边界格式非法。`
        );
      }
      if (maxText && !INT_PATTERN.test(maxText)) {
        throw new Error(
          `${data.variables[j]} 的最大垫片数“${maxText}”不是十进制整数；边界格式非法。`
        );
      }
      // Exact decimal-text comparison on normalised text (strip a sign and
      // leading zeros), never via Number.
      const minNorm = minText ? normalizeIntegerText(minText) : null;
      const maxNorm = maxText ? normalizeIntegerText(maxText) : null;
      if (minNorm && maxNorm && compareDecimalText(minNorm, maxNorm) > 0) {
        throw new Error(
          `${data.variables[j]} 的最小垫片数 ${minNorm} 大于最大垫片数 ${maxNorm}；边界格式非法。`
        );
      }
      bounds.push([minNorm, maxNorm]);
    }
    return bounds;
  }

  function clearAuditOutcome() {
    auditResult.replaceChildren();
  }

  async function runAudit() {
    let bounds;
    try {
      bounds = collectBounds();
    } catch (error) {
      setAuditStatus(error.message, "error");
      return;
    }
    if (!lastPayload) {
      setAuditStatus("当前没有可复核的原始结论，请先发起复核。", "error");
      return;
    }
    if (resultPanel.dataset.state === "stale") {
      setAuditStatus("草稿已修改，请先重新发起复核，再针对最新的可解结论发起审计。", "error");
      return;
    }
    const myGeneration = ++generation;
    if (inflight) {
      inflight.abort();
    }
    const controller = new AbortController();
    inflight = controller;
    runAuditButton.disabled = true;
    clearAuditButton.disabled = false;
    clearAuditOutcome();
    setAuditStatus("行程受限审计枚举中（精确整数，全局最优）……", "busy");
    try {
      const response = await fetch("/api/audit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...lastPayload, bounds }),
        signal: controller.signal,
      });
      const reply = await response.json().catch(() => null);
      if (myGeneration !== generation) {
        return; // 草稿变更、重新复核或取消：丢弃过期审计返回
      }
      if (!response.ok || !reply || reply.ok !== true) {
        const message = reply && reply.error ? reply.error : `HTTP ${response.status}`;
        renderAuditError(auditResult, message, response.status);
        setAuditStatus(`审计未完成：${message}`, "error");
        return;
      }
      renderAuditResult(auditResult, reply);
      setAuditStatus(
        reply.feasible
          ? "行程受限审计完成：已返回全局最优整数调整方案。"
          : "行程受限审计完成：行程内无满足全部边界的整数方案。",
        reply.feasible ? "ok" : "warn"
      );
    } catch (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      if (myGeneration !== generation) {
        return;
      }
      setAuditStatus(`审计请求失败：${error.message || error}`, "error");
    } finally {
      if (myGeneration === generation) {
        inflight = null;
        runAuditButton.disabled = false;
      }
    }
  }

  runAuditButton.addEventListener("click", runAudit);
  clearAuditButton.addEventListener("click", () => {
    generation += 1;
    if (inflight) {
      inflight.abort();
      inflight = null;
    }
    runAuditButton.disabled = false;
    minInputs.forEach((input) => {
      input.value = "";
    });
    maxInputs.forEach((input) => {
      input.value = "";
    });
    clearAuditOutcome();
    setAuditStatus("已清空边界并取消审计：原始可复核结论保持不变。", "");
  });
  // Editing bounds invalidates any in-flight audit and raises the
  // generation so a late reply can never overwrite the current draft; the
  // reviewed original conclusion above stays fresh and untouched.
  function invalidateAudit() {
    generation += 1;
    if (inflight) {
      inflight.abort();
      inflight = null;
    }
    runAuditButton.disabled = false;
    clearAuditOutcome();
    setAuditStatus("边界已修改：先前审计即使返回也将被丢弃，原始复核结论保持不变。", "warn");
  }

  [...minInputs, ...maxInputs].forEach((input) =>
    input.addEventListener("input", invalidateAudit)
  );

  setAuditStatus("可填写边界后发起行程受限审计；不填边界不影响上方既有复核。", "");
  return section;
}

function renderAuditError(parent, message, status) {
  parent.replaceChildren();
  const box = el("div", "audit-error");
  let title = "审计请求被拒绝（边界或载荷格式非法）";
  if (status === 422) {
    title = message.includes("枚举") || message.includes("节点")
      ? "行程整数格点枚举超过安全上限"
      : "边界不足以限定全部自由校正方向（请求未被受理）";
  }
  box.appendChild(el("p", "verdict bad", title));
  box.appendChild(el("p", "explanation", message));
  box.appendChild(
    el(
      "p",
      "hint",
      "上方原始方程的精确复核结论仍然保留，可继续查看每项乘积与单约束复算。"
    )
  );
  parent.appendChild(box);
}

function renderAuditResult(parent, data) {
  parent.replaceChildren();

  if (!data.solvable) {
    const box = el("div", "audit-blocked");
    box.appendChild(
      el("p", "verdict bad", "原因：原始方程本身无整数解，行程边界无从审计。")
    );
    const ob = data.obstruction;
    box.appendChild(
      el(
        "p",
        "num explanation",
        `规范除尽障碍（${ob.type === "non_divisible" ? "主元除尽失败" : "零行目标非零"}）：` +
          `变换后第 ${ob.row + 1} 行主元 ${ob.pivot}，变换后目标 ${ob.transformedTarget}` +
          `，余数 ${ob.remainder}。原始无解证据见上方结论，未被覆盖。`
      )
    );
    parent.appendChild(box);
    return;
  }

  if (!data.feasible) {
    const box = el("div", "audit-blocked");
    box.appendChild(
      el(
        "p",
        "verdict bad",
        "原因：原始方程可解，但装配行程内不存在满足全部耦合位移的整数方案。"
      )
    );
    box.appendChild(
      el(
        "p",
        "explanation",
        "已在由特解与齐次整数解格给出的全部边界整数格点上完成精确枚举" +
          `（枚举节点 ${data.nodes}），未找到同时满足原始方程与全部边界的点；` +
          "这不是连续解取整失败，也不是沿单个自由方向贪心受限。"
      )
    );
    const table = el("table", "num");
    const head = el("tr");
    ["校正项", "当前精确校正量", "最小垫片数", "最大垫片数", "当前是否在行程内"].forEach(
      (title) => head.appendChild(el("th", null, title))
    );
    table.appendChild(head);
    data.items.forEach((item) => {
      const row = el("tr");
      row.appendChild(el("td", null, item.variable));
      row.appendChild(el("td", null, item.current));
      row.appendChild(el("td", null, item.min === null ? "不限" : item.min));
      row.appendChild(el("td", null, item.max === null ? "不限" : item.max));
      row.appendChild(
        el("td", item.within ? "ok-text" : "bad-text", item.within ? "在界内" : "越界")
      );
      table.appendChild(row);
    });
    box.appendChild(table);
    box.appendChild(
      el("p", "hint", "当前精确校正量、齐次解格与 Smith 摘要等原始复核结论均保留在上方。")
    );
    parent.appendChild(box);
    return;
  }

  const box = el("div", "audit-ok");
  box.appendChild(
    el(
      "p",
      "verdict ok",
      "审计结论：原始方程与全部边界同时成立，以下为总绝对偏移最小的全局最优整数调整。"
    )
  );
  box.appendChild(
    el(
      "p",
      "num explanation",
      `目标 = Σ |调整量 − 当前精确校正量| = ${data.totalAbsOffset}` +
        `（在有界整数解格上经完备枚举证明最优，枚举节点 ${data.nodes}）。`
    )
  );

  const table = el("table", "num audit-items");
  const head = el("tr");
  [
    "校正项",
    "边界（最小 ~ 最大）",
    "当前精确校正量",
    "调整后校正量",
    "偏移（带符号）",
    "|偏移|",
    "是否在界内",
  ].forEach((title) => head.appendChild(el("th", null, title)));
  table.appendChild(head);
  data.items.forEach((item) => {
    const row = el("tr");
    const boundText =
      item.min === null && item.max === null
        ? "不限"
        : `${item.min === null ? "-∞" : item.min} ~ ${item.max === null ? "+∞" : item.max}`;
    row.appendChild(el("td", null, item.variable));
    row.appendChild(el("td", null, boundText));
    row.appendChild(el("td", null, item.current));
    row.appendChild(el("td", null, item.adjusted));
    row.appendChild(el("td", null, item.offset));
    row.appendChild(el("td", null, item.absOffset));
    row.appendChild(
      el("td", item.within ? "ok-text" : "bad-text", item.within ? "在界内 ✓" : "越界 ✗")
    );
    table.appendChild(row);
  });
  box.appendChild(table);
  box.appendChild(
    el(
      "p",
      "num",
      `调整后向量 [${data.adjusted.join(", ")}]，偏移向量 [${data.offsets.join(", ")}]` +
        "（并列时按变量标识顺序的偏移向量字典序稳定裁决）。"
    )
  );

  box.appendChild(el("h4", null, "每条原始约束在调整后方案上的精确复算"));
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
  box.appendChild(listBox);
  box.appendChild(detail);
  const first = listBox.querySelector(".constraint-item");
  if (first) {
    first.click();
  }
  parent.appendChild(box);
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
