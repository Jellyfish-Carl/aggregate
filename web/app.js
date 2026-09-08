const defaults = {
  event: "D-1", risk: 0.25, cvarEnabled: true, annualPrice: 405, monthlyPrice: 416, tenDayPrice: 424,
  annualCoverage: 0.80,
  monthlyCoverage: 0.95, tenDayCoverage: 0.98, d3Coverage: 1, d2Coverage: 1,
  quantile: "P50", adjustment: 8000,
  allowSell: true, rollingPriceEdgeLower: 100, rollingPriceEdgeUpper: 200, rollingMinFillRatio: 0.10,
  rollingMaxFillRatio: 0.20, rtPeriod: 76,
  loadScenarioSeed: 2026091501, priceScenarioSeed: 2026091502,
};
const state = {
  ...defaults,
  data: null,
  forecastPhaseId: null,
  mockPhaseId: null,
  mockTab: "load",
  confirmedLoadOverrides: [],
  confirmedPriceOverrides: [],
  draftLoadValues: null,
  draftPriceValues: null,
  draftLoadSources: null,
  draftPriceSources: null,
  scenarioDirty: false,
  loadDraftDirty: false,
  priceDraftDirty: false,
};
let activeRequestId = 0;
let progressTimer = null;
let progressValue = 0;

const money = (value) => {
  const sign = value < 0 ? "-" : "";
  const amount = Math.abs(Number(value));
  return amount >= 10000 ? `${sign}¥ ${(amount / 10000).toFixed(2)} 万` : `${sign}¥ ${amount.toFixed(0)}`;
};
const mwh = (value, digits = 0) => `${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits })} MWh`;
const eventName = (event) => event.replace("REAL_TIME", "实时").replace("MONTH_END", "月末");

function setTimelineDisabled(disabled) {
  document.querySelectorAll("#timeline button").forEach((button) => { button.disabled = disabled; });
}

function setSolveProgress(percent, label = "") {
  const root = document.getElementById("solve-progress");
  const fill = document.getElementById("solve-progress-fill");
  const number = document.getElementById("solve-progress-percent");
  const text = document.getElementById("solve-progress-label");
  const value = Math.max(0, Math.min(100, percent));
  root.hidden = false;
  fill.style.width = `${value.toFixed(1)}%`;
  number.textContent = `${Math.round(value)}%`;
  if (label) text.textContent = label;
}

function beginSolveProgress() {
  clearInterval(progressTimer);
  progressValue = 4;
  document.body.classList.add("is-solving");
  setTimelineDisabled(true);
  setSolveProgress(progressValue, `${eventName(state.event)} 运算中`);
  progressTimer = setInterval(() => {
    const increment = progressValue < 72 ? 3.2 : progressValue < 90 ? 1.1 : 0.22;
    progressValue = Math.min(96, progressValue + increment);
    setSolveProgress(progressValue);
  }, 180);
}

function finishSolveProgress() {
  clearInterval(progressTimer);
  return new Promise((resolve) => {
    const completeTimer = setInterval(() => {
      progressValue = Math.min(100, progressValue + Math.max(1.8, (100 - progressValue) * 0.35));
      setSolveProgress(progressValue);
      if (progressValue >= 100) {
        clearInterval(completeTimer);
        setTimeout(() => {
          document.getElementById("solve-progress").hidden = true;
          document.body.classList.remove("is-solving");
          setTimelineDisabled(false);
          resolve();
        }, 160);
      }
    }, 35);
  });
}

function cancelSolveProgress() {
  clearInterval(progressTimer);
  document.getElementById("solve-progress").hidden = true;
  document.body.classList.remove("is-solving");
  setTimelineDisabled(false);
}

const svgNode = (tag, attrs = {}, text = "") => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text) node.textContent = text;
  return node;
};
const path = (points) => points.map((point, index) => `${index ? "L" : "M"}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(" ");

function tooltip(event, html) {
  const root = document.getElementById("tooltip");
  root.innerHTML = html;
  root.classList.add("visible");
  root.style.left = `${Math.min(window.innerWidth - root.offsetWidth - 12, event.clientX + 12)}px`;
  root.style.top = `${Math.max(12, Math.min(window.innerHeight - root.offsetHeight - 12, event.clientY - root.offsetHeight / 2))}px`;
}
function hideTooltip() { document.getElementById("tooltip").classList.remove("visible"); }

function axes(svg, width, height, margin, maxY, unit) {
  const innerH = height - margin.top - margin.bottom;
  for (let ratio = 0; ratio <= 1.001; ratio += 0.25) {
    const y = margin.top + innerH * (1 - ratio);
    svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "grid-line" }));
    svg.appendChild(svgNode("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", class: "tick" }, (maxY * ratio).toFixed(maxY < 20 ? 1 : 0)));
  }
  [0, 6, 12, 18, 24].forEach((hour) => {
    const x = margin.left + hour / 24 * (width - margin.left - margin.right);
    svg.appendChild(svgNode("text", { x, y: height - 11, "text-anchor": hour === 0 ? "start" : hour === 24 ? "end" : "middle", class: "tick" }, `${String(hour).padStart(2, "0")}:00`));
  });
  svg.appendChild(svgNode("text", { x: margin.left, y: 13, class: "axis-label" }, unit));
}

function renderTimeline(items) {
  const root = document.getElementById("timeline");
  root.replaceChildren();
  items.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `timeline-step ${item.state}`;
    button.setAttribute("aria-current", item.state === "active" ? "step" : "false");
    button.innerHTML = `<span>${item.layer}</span><em>${item.published_at.slice(5, 16).replace("T", " ")}</em><strong>${item.label}</strong><small>${item.action}</small>`;
    button.addEventListener("click", () => {
      state.event = item.event;
      state.forecastPhaseId = null;
      state.mockPhaseId = null;
      refresh().catch(showError);
    });
    root.appendChild(button);
  });
}

function renderAction(data) {
  const action = data.current_action;
  document.getElementById("action-event").textContent = data.meta.selected_event.replace("REAL_TIME", "RT").replace("MONTH_END", "月末");
  document.getElementById("action-label").textContent = action.label;
  document.getElementById("action-reason").textContent = action.reason;
  document.getElementById("action-quantity").textContent = action.side === "MIXED"
    ? `买 ${mwh(action.buy_quantity_mwh || 0, 1)} / 卖 ${mwh(action.sell_quantity_mwh || 0, 1)}`
    : mwh(action.quantity_mwh, 1);
  document.getElementById("model-status").textContent = data.meta.model_status;
  document.getElementById("engine-label").textContent = data.meta.engine;
}

function defaultForecastPhase(data) {
  const selected = data.load_forecast.selected_snapshot_id;
  return data.load_forecast.phases.find((phase) => phase.snapshot_id === selected)
    || data.load_forecast.phases[5]
    || data.load_forecast.phases[0];
}

function renderForecast(data) {
  const phases = data.load_forecast.phases;
  const defaultPhase = defaultForecastPhase(data);
  let active = phases.find((phase) => phase.snapshot_id === state.forecastPhaseId);
  if (!active) {
    active = defaultPhase;
    state.forecastPhaseId = active.snapshot_id;
  }
  const overview = phases.slice(0, 6);
  if (active.event === "REAL_TIME") overview.push(active);

  const controls = document.getElementById("forecast-phase-buttons");
  controls.replaceChildren();
  overview.forEach((phase) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `phase-button${phase.available ? "" : " future"}`;
    button.disabled = !phase.available;
    button.setAttribute("aria-pressed", phase.snapshot_id === active.snapshot_id ? "true" : "false");
    button.textContent = phase.available ? phase.label : `${phase.label} · 未到达`;
    if (phase.available) {
      button.addEventListener("click", () => {
        state.forecastPhaseId = phase.snapshot_id;
        renderForecast(data);
      });
    }
    controls.appendChild(button);
  });

  const svg = document.getElementById("forecast-chart");
  svg.replaceChildren();
  const width = 1100, height = 390, margin = { top: 22, right: 22, bottom: 40, left: 58 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  const maxY = Math.max(1, Math.ceil(Math.max(...active.rows.map((row) => Math.max(row.p90_mwh, row.actual_mwh ?? 0))) * 1.1));
  const pointCount = active.rows.length;
  const x = (index) => margin.left + index / Math.max(pointCount - 1, 1) * innerW;
  const y = (value) => margin.top + innerH - value / maxY * innerH;
  axes(svg, width, height, margin, maxY, "MWh / 15分钟");

  const confidenceUpper = active.rows.map((row, index) => [x(index), y(row.p90_mwh)]);
  const confidenceLower = active.rows.map((row, index) => [x(index), y(row.p10_mwh)]).reverse();
  svg.appendChild(svgNode("path", {
    d: `${path([...confidenceUpper, ...confidenceLower])} Z`,
    class: "forecast-confidence-area",
  }));
  svg.appendChild(svgNode("path", {
    d: path(active.rows.map((row, index) => [x(index), y(row.p10_mwh)])),
    class: "forecast-boundary forecast-p10-line",
  }));
  svg.appendChild(svgNode("path", {
    d: path(active.rows.map((row, index) => [x(index), y(row.p90_mwh)])),
    class: "forecast-boundary forecast-p90-line",
  }));
  svg.appendChild(svgNode("path", {
    d: path(active.rows.map((row, index) => [x(index), y(row.p50_mwh)])),
    class: "forecast-p50",
  }));
  const actualPoints = active.rows
    .map((row, index) => row.actual_mwh == null ? null : [x(index), y(row.actual_mwh)])
    .filter(Boolean);
  if (actualPoints.length) {
    svg.appendChild(svgNode("path", {
      d: path(actualPoints),
      class: "forecast-actual",
    }));
  }

  active.rows.forEach((row, index) => {
    const hit = svgNode("rect", {
      x: x(index) - innerW / (2 * pointCount),
      y: margin.top,
      width: innerW / pointCount,
      height: innerH,
      class: "hit-area",
    });
    const actualText = row.actual_mwh == null ? "未实现" : `${row.actual_mwh.toFixed(3)} MWh`;
    hit.addEventListener("mousemove", (event) => tooltip(event, `<b>${active.label} · ${row.time}</b><br>P10 ${row.p10_mwh.toFixed(3)} MWh<br>P50 ${row.p50_mwh.toFixed(3)} MWh<br>P90 ${row.p90_mwh.toFixed(3)} MWh<br>实际 ${actualText}`));
    hit.addEventListener("mouseleave", hideTooltip);
    svg.appendChild(hit);
  });

  document.getElementById("forecast-published-at").textContent = `${active.label} · ${active.published_at.slice(0, 16).replace("T", " ")}${active.available ? "" : " · 当前节点尚不可见"}`;
  document.getElementById("forecast-total").textContent = mwh(active.quality.p50_total_mwh, 1);
  document.getElementById("forecast-accuracy").textContent = active.quality.wape == null
    ? "待实现"
    : `${((1 - active.quality.wape) * 100).toFixed(2)}%`;
  document.getElementById("forecast-width").textContent = mwh(active.quality.interval_width_mwh, 1);
}

const actionText = (action) => {
  if (action.state === "FUTURE") return "待节点到达";
  if (action.side === "HOLD") return "不交易";
  if (action.side === "MIXED") return `买入 ${mwh(action.buy_quantity_mwh || 0, 1)} / 卖出 ${mwh(action.sell_quantity_mwh || 0, 1)}`;
  return `${action.side === "BUY" ? "买入" : "卖出"} ${mwh(action.quantity_mwh, 1)} @ ¥${action.execution_price_yuan_per_mwh.toFixed(0)}`;
};

function renderDecisions(data) {
  const root = document.getElementById("decision-table");
  root.replaceChildren();
  const baseline = data.portfolio.baseline.actions;
  const optimized = data.portfolio.optimized.actions;
  baseline.forEach((base, index) => {
    const opt = optimized[index];
    const row = document.createElement("tr");
    if (base.event === data.meta.selected_event) row.className = "selected-row";
    const type = base.event === "D-3" || base.event === "D-2" ? "滚撮运行日" : "中长期月度累计";
    const position = opt.state === "FUTURE" ? "—" : mwh(opt.resulting_position_mwh, 1);
    row.innerHTML = `<td><b>${base.event}</b></td><td>${type}</td><td>${actionText(base)}</td><td>${actionText(opt)}</td><td>${position}</td><td>${opt.state === "FUTURE" ? "尚未到达" : opt.reason}</td>`;
    root.appendChild(row);
  });
  const d1 = document.createElement("tr");
  if (data.meta.selected_event === "D-1") d1.className = "selected-row";
  d1.innerHTML = `<td><b>D-1</b></td><td>96点日前申报</td><td>—</td><td>申报 ${mwh(data.declaration.total_mwh, 1)}</td><td>96点合计</td><td>${data.declaration.status === "LOCKED" ? "已锁定" : "条件计划"}</td>`;
  root.appendChild(d1);
  const assessment = data.portfolio.assessment;
  document.getElementById("annual-assessment").textContent = `年度逐点 最低 ${(assessment.annual.min_ratio * 100).toFixed(1)}% · ${assessment.annual.compliant_periods}/48点通过 · ${assessment.annual.status}`;
  document.getElementById("overall-assessment").textContent = assessment.overall.status === "PENDING"
    ? "总体中长期：待滚撮完成"
    : `总体逐点 ${(assessment.overall.min_ratio * 100).toFixed(1)}%-${(assessment.overall.max_ratio * 100).toFixed(1)}% · ${assessment.overall.compliant_periods}/48点通过 · ${assessment.overall.status}`;
  const rule = data.portfolio.rolling_rule;
  document.getElementById("rolling-rule-summary").textContent = `现货P50价差下限 ¥${Number(rule.price_edge_lower_yuan_per_mwh).toFixed(0)} · 上限 ¥${Number(rule.price_edge_upper_yuan_per_mwh).toFixed(0)} · 仓位 ${(Number(rule.position_lower_ratio) * 100).toFixed(0)}%–${(Number(rule.position_upper_ratio) * 100).toFixed(0)}% · 成交订单量 ${(Number(rule.min_fill_ratio) * 100).toFixed(0)}%–${(Number(rule.max_fill_ratio) * 100).toFixed(0)}%`;
  const baselineMargin = Number(data.settlement.baseline_margin_yuan);
  const optimizedMargin = Number(data.settlement.optimized_margin_yuan);
  const improvement = Number(data.settlement.margin_improvement_yuan);
  document.getElementById("baseline-margin").textContent = money(baselineMargin);
  document.getElementById("optimized-margin").textContent = money(optimizedMargin);
  const improvementNode = document.getElementById("margin-improvement");
  improvementNode.textContent = `相较交易员基线${improvement >= 0 ? "增加" : "减少"} ${money(Math.abs(improvement))}`;
  improvementNode.className = improvement >= 0 ? "positive-margin" : "negative-margin";
  renderRollingOrders(data);
}

function renderRollingOrders(data) {
  const panel = document.getElementById("rolling-orders-panel");
  const body = document.getElementById("rolling-orders-table");
  const book = data.portfolio?.rolling_orders;
  const visible = book && (data.meta.selected_event === "D-3" || data.meta.selected_event === "D-2");
  panel.hidden = !visible;
  if (!visible) return;
  body.replaceChildren();
  const orders = book.orders || [];
  const accepted = orders.filter((order) => order.accepted);
  const captured = orders.filter((order) => order.price_triggered);
  document.getElementById("rolling-orders-summary").textContent = `${book.selected_event} · 捕捉 ${captured.length}/${orders.length} 笔 · 成交 ${accepted.length} 笔`;
  orders.forEach((order) => {
    const row = document.createElement("tr");
    if (order.accepted) row.className = "selected-row";
    const fill = order.accepted
      ? `${mwh(order.accepted_quantity_mwh, 3)} · ${(Number(order.accepted_fill_ratio) * 100).toFixed(1)}%`
      : "—";
    row.innerHTML = `<td>${order.arrival_sequence}</td><td><b>${order.order_id}</b></td><td>${order.time}</td><td>${order.our_side === "BUY" ? "买入" : "卖出"}</td><td>${mwh(order.quantity_mwh, 2)}</td><td>¥${Number(order.price_yuan_per_mwh).toFixed(1)}</td><td>¥${Number(order.spot_forecast_price_yuan_per_mwh).toFixed(1)}</td><td>¥${Number(order.price_edge_yuan_per_mwh).toFixed(1)}</td><td>${fill}</td><td>${order.accepted ? "已成交" : order.price_triggered ? "已捕捉/容量不足" : "未捕捉"}</td><td>${order.decision_reason}</td>`;
    body.appendChild(row);
  });
}

function renderDeclaration(data) {
  const rows = data.declaration_breakdown.rows;
  const svg = document.getElementById("declaration-chart");
  svg.replaceChildren();
  const selectedEvent = data.meta.selected_event;
  const exposureOnly = ["ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2"].includes(selectedEvent);
  const l1Stage = ["ANNUAL", "MONTHLY", "TEN_DAY"].includes(selectedEvent);
  document.querySelectorAll(".pre-spot-only").forEach((node) => { node.hidden = !exposureOnly; });
  document.querySelectorAll(".spot-stage-only").forEach((node) => { node.hidden = exposureOnly; });
  const width = 1100, height = 420, margin = { top: 22, right: 72, bottom: 40, left: 58 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  const contractKeys = ["annual_mwh", "monthly_mwh", "ten_day_mwh", "d3_mwh", "d2_mwh"];
  const positiveKeys = exposureOnly
    ? [...contractKeys, "spot_exposure_buy_mwh"]
    : [...contractKeys, "day_ahead_buy_mwh", "real_time_buy_mwh"];
  const positiveMax = Math.max(...rows.map((row) => positiveKeys.reduce((sum, key) => sum + Math.max(Number(row[key] || 0), 0), 0)));
  const negativeMax = Math.max(...rows.map((row) => {
    const contractSell = contractKeys.reduce((sum, key) => sum + Math.max(-Number(row[key] || 0), 0), 0);
    return contractSell + (exposureOnly
      ? Number(row.spot_exposure_sell_mwh || 0)
      : Number(row.day_ahead_sell_mwh || 0) + Number(row.real_time_sell_mwh || 0));
  }));
  const lineMax = Math.max(...rows.flatMap((row) => exposureOnly
    ? [Number(row.forecast_load_mwh || 0)]
    : [Number(row.actual_load_mwh || 0), Number(row.forecast_load_mwh || 0)]));
  const maxY = Math.max(1, Math.ceil(Math.max(positiveMax, lineMax) * 1.12));
  const minY = negativeMax > 0.0005
    ? -Math.max(0.1, Math.ceil(negativeMax * 11.5) / 10)
    : 0;
  const x = (index) => margin.left + index / Math.max(rows.length - 1, 1) * innerW;
  const y = (value) => margin.top + (maxY - value) / (maxY - minY) * innerH;
  const tickValues = [maxY, maxY * 0.75, maxY * 0.5, maxY * 0.25, 0];
  if (minY < 0) tickValues.push(minY);
  tickValues.forEach((value) => {
    const yValue = y(value);
    svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: yValue, y2: yValue, class: Math.abs(value) < 1e-9 ? "declaration-zero-axis" : "grid-line" }));
    svg.appendChild(svgNode("text", { x: margin.left - 10, y: yValue + 4, "text-anchor": "end", class: "tick" }, value.toFixed(Math.max(maxY, Math.abs(minY)) < 20 ? 1 : 0)));
  });
  [0, 6, 12, 18, 24].forEach((hour) => {
    const hourX = margin.left + hour / 24 * innerW;
    svg.appendChild(svgNode("text", { x: hourX, y: height - 11, "text-anchor": hour === 0 ? "start" : hour === 24 ? "end" : "middle", class: "tick" }, `${String(hour).padStart(2, "0")}:00`));
  });
  svg.appendChild(svgNode("text", { x: margin.left, y: 13, class: "axis-label" }, exposureOnly
    ? "MWh / 15分钟；预测现货敞口正值为预计买入，负值为预计卖出"
    : "MWh / 15分钟；买入 +，卖出 -"));
  const columnWidth = innerW / rows.length * 0.72;
  const drawSegment = (index, start, amount, className) => {
    if (Math.abs(amount) < 0.0005) return start;
    const end = start + amount;
    const yStart = y(start); const yEnd = y(end);
    svg.appendChild(svgNode("rect", { x: x(index) - columnWidth / 2, y: Math.min(yStart, yEnd), width: columnWidth, height: Math.max(1, Math.abs(yEnd - yStart)), class: className }));
    return end;
  };
  const classByKey = {
    annual_mwh: "declaration-long-term annual",
    monthly_mwh: "declaration-long-term monthly",
    ten_day_mwh: "declaration-long-term ten-day",
    d3_mwh: "declaration-near-term",
    d2_mwh: "declaration-near-term",
    spot_exposure_buy_mwh: "declaration-spot-exposure",
    day_ahead_buy_mwh: "declaration-day-ahead-buy",
    real_time_buy_mwh: "declaration-realtime-buy",
  };
  rows.forEach((row, index) => {
    let positive = 0;
    let negative = 0;
    positiveKeys.forEach((key) => {
      positive = drawSegment(index, positive, Math.max(Number(row[key] || 0), 0), classByKey[key]);
    });
    contractKeys.forEach((key) => {
      negative = drawSegment(index, negative, Math.min(Number(row[key] || 0), 0), classByKey[key]);
    });
    if (exposureOnly) {
      negative = drawSegment(index, negative, -Number(row.spot_exposure_sell_mwh || 0), "declaration-spot-exposure");
    } else {
      negative = drawSegment(index, negative, -Number(row.day_ahead_sell_mwh || 0), "declaration-day-ahead-sell");
      negative = drawSegment(index, negative, -Number(row.real_time_sell_mwh || 0), "declaration-realtime-sell");
    }
    const hit = svgNode("rect", { x: x(index) - columnWidth / 2, y: margin.top, width: columnWidth, height: innerH, class: "hit-area" });
    const contractHtml = `年度 ${row.annual_mwh.toFixed(3)} MWh · 月度 ${row.monthly_mwh.toFixed(3)} MWh · 旬 ${row.ten_day_mwh.toFixed(3)} MWh<br>D-3 ${row.d3_mwh.toFixed(3)} MWh · D-2 ${row.d2_mwh.toFixed(3)} MWh<br>年度逐点比例 ${(row.annual_assessment_ratio * 100).toFixed(1)}% · ${row.annual_assessment_compliant ? "通过" : "低于60%"}<br>总体逐点比例 ${(row.overall_assessment_ratio * 100).toFixed(1)}% · ${row.overall_assessment_compliant ? "通过" : "超出90%-110%"}`;
    const exposure = Number(row.spot_exposure_mwh || 0);
    const detailHtml = exposureOnly
      ? `${contractHtml}<br>P50预测负荷 ${row.forecast_load_mwh.toFixed(3)} MWh<br>预测现货敞口 ${Math.abs(exposure).toFixed(3)} MWh · ${exposure >= 0 ? "预计买入" : "预计卖出"}<br><small>预测现货敞口 = P50预测负荷 - 已锁定中长期；此阶段不区分日前和实时。</small>`
      : `${contractHtml}<br>日前买入 ${row.day_ahead_buy_mwh.toFixed(3)} MWh · 日前卖出 ${row.day_ahead_sell_mwh.toFixed(3)} MWh<br>实时买入 ${row.real_time_buy_mwh.toFixed(3)} MWh · 实时卖出 ${row.real_time_sell_mwh.toFixed(3)} MWh<br>P50负荷 ${row.forecast_load_mwh.toFixed(3)} MWh · 实际负荷 ${row.actual_load_mwh.toFixed(3)} MWh<br>储能后电网负荷 ${row.net_grid_load_after_storage_mwh.toFixed(3)} MWh<br><small>中长期年度及总体比例按对应半小时点逐点考核。<br>中长期 + 日前买 - 日前卖 + 实时买 - 实时卖 = 储能后电网负荷</small>`;
    hit.addEventListener("mousemove", (event) => tooltip(event, `<b>${row.time}</b><br>${detailHtml}`));
    hit.addEventListener("mouseleave", hideTooltip);
    svg.appendChild(hit);
  });
  const longTermLayers = [["annual_mwh", "年"], ["monthly_mwh", "月"], ["ten_day_mwh", "旬"]];
  rows.forEach((row, index) => {
    const presentLayers = longTermLayers
      .map(([key, label]) => ({ key, label, amount: Math.max(Number(row[key] || 0), 0) }))
      .filter((layer) => layer.amount >= 0.0005);
    let cumulative = 0;
    presentLayers.forEach((layer, layerIndex) => {
      cumulative += layer.amount;
      if (layerIndex === presentLayers.length - 1) return;
      svg.appendChild(svgNode("line", {
        x1: x(index) - columnWidth / 2,
        x2: x(index) + columnWidth / 2,
        y1: y(cumulative),
        y2: y(cumulative),
        class: "declaration-layer-boundary",
        "data-layer": layer.key,
      }));
    });
  });
  svg.appendChild(svgNode("text", { x: width - margin.right + 8, y: margin.top + 14, class: "declaration-layer-label" }, "下→上"));
  svg.appendChild(svgNode("text", { x: width - margin.right + 8, y: margin.top + 29, class: "declaration-layer-label" }, "年/月/旬"));
  svg.appendChild(svgNode("path", { d: path(rows.map((row, index) => [x(index), y(row.forecast_load_mwh)])), class: "declaration-forecast" }));
  if (!exposureOnly) {
    svg.appendChild(svgNode("path", { d: path(rows.map((row, index) => [x(index), y(row.actual_load_mwh)])), class: "declaration-actual" }));
    rows.forEach((row, index) => svg.appendChild(svgNode("circle", { cx: x(index), cy: y(row.actual_load_mwh), r: 2.2, class: "declaration-actual-point" })));
  }
  document.getElementById("declaration-kicker").textContent = exposureOnly ? "LONG-TERM POSITION & SPOT EXPOSURE" : "DAY-AHEAD ENERGY STACK";
  document.getElementById("declaration-title").textContent = exposureOnly ? "2. 中长期交易与预测现货敞口" : "2. D-1 96 点申报电量";
  svg.setAttribute("aria-label", exposureOnly
    ? "中长期合同与基于P50负荷预测的单一现货敞口"
    : "96个15分钟中长期、日前申报、实时偏差、预测负荷和实际负荷");
  document.getElementById("declaration-status").textContent = exposureOnly ? "预测敞口" : data.declaration.status === "LOCKED" ? "已锁定" : "预测方案";
  document.getElementById("declaration-status").className = `status-label ${!exposureOnly && data.declaration.status === "LOCKED" ? "locked" : ""}`;
  document.getElementById("declaration-longterm-total").textContent = mwh(data.declaration_breakdown.totals.long_term_mwh, 1);
  document.getElementById("declaration-exposure-total").textContent = `${mwh(data.declaration_breakdown.totals.spot_exposure_buy_mwh, 1)} / ${mwh(data.declaration_breakdown.totals.spot_exposure_sell_mwh, 1)}`;
  document.getElementById("declaration-forecast-total").textContent = mwh(data.declaration_breakdown.totals.forecast_load_mwh, 1);
  document.getElementById("declaration-total").textContent = mwh(data.declaration.total_mwh, 1);
  document.getElementById("declaration-dayahead-total").textContent = `${mwh(data.declaration_breakdown.totals.day_ahead_buy_mwh, 1)} / ${mwh(data.declaration_breakdown.totals.day_ahead_sell_mwh, 1)}`;
  document.getElementById("declaration-realtime-total").textContent = `${mwh(data.declaration_breakdown.totals.real_time_buy_mwh, 1)} / ${mwh(data.declaration_breakdown.totals.real_time_sell_mwh, 1)}`;
  document.getElementById("declaration-grid-total").textContent = mwh(data.declaration_breakdown.totals.net_grid_load_after_storage_mwh, 1);
  document.getElementById("declaration-actual-total").textContent = mwh(data.declaration_breakdown.totals.actual_load_mwh, 1);
  const guard = data.declaration_breakdown.assessment_guard;
  document.getElementById("declaration-assessment-guard").textContent = `中长期48点考核 ${guard.status === "PASS" ? "全部通过" : guard.status === "PENDING" ? "待滚撮完成" : "存在不合规点"}`;
  const l3Objective = data.l3_objective || data.l2_objective;
  document.getElementById("l3-objective-summary").textContent = l3Objective.milp_executed
    ? `L3 MILP目标 ${money(l3Objective.objective_yuan)} · gap ${((l3Objective.mip_gap || 0) * 100).toFixed(3)}%`
    : `诊断参考策略 ${money(l3Objective.objective_yuan)}`;
  document.getElementById("l3-objective-container").hidden = l1Stage;
  document.getElementById("declaration-deadline-container").hidden = exposureOnly;
  document.getElementById("declaration-deadline").textContent = data.declaration.deadline;
}

function renderStorage(data) {
  const rows = data.storage.rows;
  const svg = document.getElementById("storage-chart");
  svg.replaceChildren();
  const width = 1100, height = 500, margin = { top: 42, right: 76, bottom: 48, left: 72 };
  const innerW = width - margin.left - margin.right;
  const mainH = 300;
  const actionGap = 32;
  const actionH = height - margin.top - margin.bottom - mainH - actionGap;
  const actionTop = margin.top + mainH + actionGap;
  const actionBottom = actionTop + actionH;
  const actionBaseline = actionTop + actionH / 2;
  const socStarts = rows.map((row) => Number(row.soc_start_mwh));
  const socValues = rows.map((row) => Number(row.soc_mwh));
  const intervalActions = rows.map((row) => Number(row.charge_from_grid_mwh) - Number(row.discharge_to_load_mwh));
  const prices = rows.map((row) => Number(row.real_time_price));

  const niceDomain = (values, includeZero = false) => {
    let min = Math.min(...values, ...(includeZero ? [0] : []));
    let max = Math.max(...values, ...(includeZero ? [0] : []));
    const rawSpan = Math.max(max - min, Math.abs(max) * 0.1, 0.1);
    min -= rawSpan * 0.1;
    max += rawSpan * 0.1;
    const roughStep = (max - min) / 5;
    const power = 10 ** Math.floor(Math.log10(roughStep));
    const normalized = roughStep / power;
    const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10) * power;
    return {
      min: Math.floor(min / step) * step,
      max: Math.ceil(max / step) * step,
      step,
    };
  };
  const rawEnergyDomain = niceDomain([...socStarts, ...socValues]);
  const energyDomain = { ...rawEnergyDomain, min: Math.max(0, rawEnergyDomain.min) };
  const priceDomain = niceDomain(prices);
  const ratedIntervalEnergy = Number(data.storage.power_mw) * Number(data.storage.interval_minutes) / 60;
  const actualPeakAction = Math.max(...intervalActions.map((value) => Math.abs(value)), 0.01);
  const actionMax = niceDomain([0, actualPeakAction], true).max;
  const peakPowerUtilization = actualPeakAction / Math.max(ratedIntervalEnergy, 0.01);
  const x = (index) => margin.left + index / Math.max(rows.length - 1, 1) * innerW;
  const yEnergy = (value) => margin.top + (energyDomain.max - value) / (energyDomain.max - energyDomain.min) * mainH;
  const yPrice = (value) => margin.top + (priceDomain.max - value) / (priceDomain.max - priceDomain.min) * mainH;
  const yAction = (value) => actionBaseline - value / actionMax * actionH / 2;

  svg.appendChild(svgNode("rect", { x: margin.left, y: margin.top, width: innerW, height: mainH, class: "storage-plot-bg" }));
  const energyDigits = energyDomain.step < 1 ? 2 : 1;
  for (let value = energyDomain.min; value <= energyDomain.max + energyDomain.step / 2; value += energyDomain.step) {
    const y = yEnergy(value);
    svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "grid-line" }));
    svg.appendChild(svgNode("text", { x: margin.left - 12, y: y + 4, "text-anchor": "end", class: "tick storage-energy-tick" }, value.toFixed(energyDigits)));
  }
  for (let value = priceDomain.min; value <= priceDomain.max + priceDomain.step / 2; value += priceDomain.step) {
    const y = yPrice(value);
    svg.appendChild(svgNode("line", { x1: width - margin.right, x2: width - margin.right + 5, y1: y, y2: y, class: "storage-price-tick-mark" }));
    svg.appendChild(svgNode("text", { x: width - margin.right + 11, y: y + 4, class: "tick storage-price-tick" }, value.toFixed(0)));
  }
  [0, 6, 12, 18, 24].forEach((hour) => {
    const tickX = margin.left + hour / 24 * innerW;
    svg.appendChild(svgNode("text", { x: tickX, y: height - 15, "text-anchor": hour === 0 ? "start" : hour === 24 ? "end" : "middle", class: "tick" }, `${String(hour).padStart(2, "0")}:00`));
  });
  svg.appendChild(svgNode("text", { x: margin.left, y: 20, class: "axis-label storage-energy-label" }, "储能电量 / MWh"));
  svg.appendChild(svgNode("text", { x: width - margin.right, y: 20, "text-anchor": "end", class: "axis-label storage-price-label" }, "实时电价 / 元·MWh⁻¹"));

  svg.appendChild(svgNode("rect", { x: margin.left, y: actionTop, width: innerW, height: actionH, class: "storage-action-bg" }));
  svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: actionBaseline, y2: actionBaseline, class: "storage-action-baseline" }));
  const actionDigits = actionMax < 1 ? 2 : 1;
  svg.appendChild(svgNode("text", { x: margin.left, y: actionTop - 10, class: "axis-label storage-action-label" }, "时段充放电量 / MWh（局部缩放）"));
  svg.appendChild(svgNode("text", { x: width - margin.right, y: actionTop - 10, "text-anchor": "end", class: "storage-action-limit" }, `额定单点上限 ±${ratedIntervalEnergy.toFixed(2)} MWh`));
  svg.appendChild(svgNode("text", { x: margin.left - 12, y: actionTop + 4, "text-anchor": "end", class: "tick storage-action-tick charge-tick" }, `+${actionMax.toFixed(actionDigits)}`));
  svg.appendChild(svgNode("text", { x: margin.left - 12, y: actionBaseline + 4, "text-anchor": "end", class: "tick storage-action-tick" }, "0"));
  svg.appendChild(svgNode("text", { x: margin.left - 12, y: actionBottom + 4, "text-anchor": "end", class: "tick storage-action-tick discharge-tick" }, `−${actionMax.toFixed(actionDigits)}`));

  const columnWidth = Math.max(5, innerW / rows.length * 0.66);
  rows.forEach((row, index) => {
    const intervalAction = intervalActions[index];
    if (Math.abs(intervalAction) > 0.0005) {
      const actionY = yAction(intervalAction);
      svg.appendChild(svgNode("rect", {
        x: x(index) - columnWidth / 2,
        y: Math.min(actionBaseline, actionY),
        width: columnWidth,
        height: Math.max(2, Math.abs(actionY - actionBaseline)),
        rx: 1.5,
        class: intervalAction > 0 ? "storage-action charge" : "storage-action discharge",
      }));
    }
    const hitWidth = Math.max(10, innerW / rows.length);
    const hit = svgNode("rect", { x: x(index) - hitWidth / 2, y: margin.top, width: hitWidth, height: actionBottom - margin.top, class: "hit-area" });
    const mode = { CHARGE: "充电", DISCHARGE: "放电", IDLE: "等待" }[row.mode];
    const socChange = socValues[index] - socStarts[index];
    const changeText = socChange > 0.0005
      ? `储能电量增加 ${socChange.toFixed(3)} MWh`
      : socChange < -0.0005
        ? `储能电量减少 ${Math.abs(socChange).toFixed(3)} MWh`
        : "储能电量不变";
    hit.addEventListener("mousemove", (event) => tooltip(event, `<b>${row.time} · ${mode}</b><br>储能总电量 ${row.soc_mwh.toFixed(3)} MWh<br>${changeText}<br>电网充电 ${row.charge_from_grid_mwh.toFixed(3)} MWh · 实际充入 ${row.charge_stored_mwh.toFixed(3)} MWh<br>负荷侧放电 ${row.discharge_to_load_mwh.toFixed(3)} MWh · 实际从储能放出 ${row.discharge_from_soc_mwh.toFixed(3)} MWh<br>实时电价 ¥${Number(row.real_time_price).toFixed(2)} / MWh<br>SOC ${row.soc_start_mwh.toFixed(3)} → ${row.soc_mwh.toFixed(3)} MWh`));
    hit.addEventListener("mouseleave", hideTooltip);
    svg.appendChild(hit);
  });
  svg.appendChild(svgNode("path", { d: path(rows.map((row, index) => [x(index), yPrice(row.real_time_price)])), class: "storage-price" }));
  svg.appendChild(svgNode("path", { d: path(socValues.map((value, index) => [x(index), yEnergy(value)])), class: "storage-soc" }));
  const storageMode = data.meta.storage_visibility?.mode;
  if (storageMode === "REAL_TIME_MPC") {
    const selected = Math.max(0, Math.min(rows.length - 1, state.rtPeriod - 1));
    svg.appendChild(svgNode("line", { x1: x(selected), x2: x(selected), y1: margin.top, y2: actionBottom, class: "current-marker" }));
    svg.appendChild(svgNode("circle", { cx: x(selected), cy: yEnergy(socValues[selected]), r: 4.5, class: "storage-selected-point soc-point" }));
    svg.appendChild(svgNode("circle", { cx: x(selected), cy: yPrice(prices[selected]), r: 4, class: "storage-selected-point price-point" }));
    const markerLabelX = Math.min(width - margin.right - 24, Math.max(margin.left + 24, x(selected)));
    svg.appendChild(svgNode("rect", { x: markerLabelX - 24, y: margin.top + 8, width: 48, height: 21, rx: 3, class: "current-marker-label-bg" }));
    svg.appendChild(svgNode("text", { x: markerLabelX, y: margin.top + 23, "text-anchor": "middle", class: "current-marker-label" }, rows[selected].time));
    const current = data.storage.current;
    const currentAmount = current.mode === "CHARGE" ? current.charge_from_grid_mwh : current.discharge_to_load_mwh;
    document.getElementById("storage-current-action").innerHTML = `<small>当前动作</small><b>${current.time} · ${{ CHARGE: "充电", DISCHARGE: "放电", IDLE: "等待" }[current.mode]} ${mwh(currentAmount, 2)}</b><em>额定 ${data.storage.power_mw.toFixed(1)} MW · 峰值利用率 ${(peakPowerUtilization * 100).toFixed(0)}%</em>`;
    document.getElementById("storage-current-soc").innerHTML = `<small>储能状态</small><b>SOC ${mwh(current.soc_start_mwh, 2)} → ${mwh(current.soc_mwh, 2)}</b><em>容量 ${mwh(data.storage.capacity_mwh, 1)} · 效率 ${data.storage.efficiency.toFixed(2)}</em>`;
  } else {
    const isPlan = storageMode === "DAY_AHEAD_CONDITIONAL_PLAN";
    const startSoc = rows[0].soc_start_mwh;
    const endSoc = rows[rows.length - 1].soc_mwh;
    document.getElementById("storage-current-action").innerHTML = isPlan
      ? `<small>计划性质</small><b>全天96点条件计划 · 尚未执行</b><em>额定 ${data.storage.power_mw.toFixed(1)} MW · 峰值利用率 ${(peakPowerUtilization * 100).toFixed(0)}%</em>`
      : `<small>执行状态</small><b>全天96点已执行 · 月末复盘</b><em>额定 ${data.storage.power_mw.toFixed(1)} MW · 峰值利用率 ${(peakPowerUtilization * 100).toFixed(0)}%</em>`;
    document.getElementById("storage-current-soc").innerHTML = `<small>${isPlan ? "计划SOC" : "执行SOC"}</small><b>${mwh(startSoc, 2)} → ${mwh(endSoc, 2)}</b><em>容量 ${mwh(data.storage.capacity_mwh, 1)} · 效率 ${data.storage.efficiency.toFixed(2)}</em>`;
  }
  const summaryLabel = storageMode === "DAY_AHEAD_CONDITIONAL_PLAN" ? "计划汇总" : "全天汇总";
  document.getElementById("storage-day-summary").innerHTML = `<small>${summaryLabel}</small><b>充 ${mwh(data.storage.charged_mwh, 1)} · 放 ${mwh(data.storage.discharged_mwh, 1)}</b><em>价差价值 ${money(data.storage.gross_arbitrage_value_yuan)}</em>`;
}

function scenarioRows(data, kind) {
  return data.scenario_editor?.[kind]?.rows || [];
}

function syncScenarioStateFromData(data) {
  const loadRows = scenarioRows(data, "load");
  const priceRows = scenarioRows(data, "real_time_price");
  if (!state.loadDraftDirty || !state.draftLoadValues || state.draftLoadValues.length !== loadRows.length) {
    state.loadScenarioSeed = Number(data.scenario_editor?.load?.seed ?? state.loadScenarioSeed);
    state.confirmedLoadOverrides = loadRows.map((row) => row.source === "MANUAL_OVERRIDE" ? Number(row.actual) : null);
    state.draftLoadValues = loadRows.map((row) => Number(row.actual));
    state.draftLoadSources = loadRows.map((row) => row.source || "SEEDED_RANDOM");
  }
  if (!state.priceDraftDirty || !state.draftPriceValues || state.draftPriceValues.length !== priceRows.length) {
    state.priceScenarioSeed = Number(data.scenario_editor?.real_time_price?.seed ?? state.priceScenarioSeed);
    state.confirmedPriceOverrides = priceRows.map((row) => row.source === "MANUAL_OVERRIDE" ? Number(row.actual) : null);
    state.draftPriceValues = priceRows.map((row) => Number(row.actual));
    state.draftPriceSources = priceRows.map((row) => row.source || "SEEDED_RANDOM");
  }
  const loadSeed = document.getElementById("load-scenario-seed");
  const priceSeed = document.getElementById("price-scenario-seed");
  if (loadSeed) loadSeed.value = state.loadScenarioSeed;
  if (priceSeed) priceSeed.value = state.priceScenarioSeed;
}

function scenarioStatus(kind, rows, values, sources) {
  const manual = values.reduce((count, value, index) => count + (sources?.[index] === "MANUAL_OVERRIDE" ? 1 : 0), 0);
  const outside = values.reduce((count, value, index) => {
    const row = rows[index];
    return count + (row && (value < row.p10 || value > row.p90) ? 1 : 0);
  }, 0);
  const dirty = kind === "load" ? state.loadDraftDirty : state.priceDraftDirty;
  return `${kind === "load" ? "实际负荷" : "实际实时价格"}：${manual} 个人工点${outside ? `，${outside} 个区间外点` : ""}${dirty ? " · 待确认" : " · 已确认"}`;
}

function renderScenarioChart(id, rows, values, sources, kind) {
  const svg = document.getElementById(id);
  if (!svg || !rows.length || !values?.length) return;
  svg.replaceChildren();
  const width = 1100, height = 300, margin = { top: 18, right: 20, bottom: 34, left: 58 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;
  const allValues = rows.flatMap((row, index) => [Number(row.p10), Number(row.p50), Number(row.p90), Number(values[index])]);
  const minY = kind === "load" ? Math.min(0, Math.min(...allValues)) : Math.min(...allValues);
  const maxY = Math.max(1, Math.max(...allValues));
  const pad = Math.max((maxY - minY) * 0.08, kind === "load" ? 0.15 : 10);
  const domainMin = minY - pad, domainMax = maxY + pad;
  svg.dataset.domainMin = String(domainMin);
  svg.dataset.domainMax = String(domainMax);
  const x = (index) => margin.left + index / Math.max(rows.length - 1, 1) * innerW;
  const y = (value) => margin.top + (domainMax - Number(value)) / (domainMax - domainMin) * innerH;
  for (let ratio = 0; ratio <= 1.001; ratio += 0.25) {
    const yy = margin.top + ratio * innerH;
    svg.appendChild(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: yy, y2: yy, class: "scenario-grid-line" }));
    const value = domainMax - ratio * (domainMax - domainMin);
    svg.appendChild(svgNode("text", { x: margin.left - 9, y: yy + 4, "text-anchor": "end", class: "scenario-tick" }, kind === "load" ? value.toFixed(2) : value.toFixed(0)));
  }
  [0, 24, 48, 72, 95].forEach((index) => {
    const xx = x(index);
    svg.appendChild(svgNode("text", { x: xx, y: height - 10, "text-anchor": index === 0 ? "start" : index === 95 ? "end" : "middle", class: "scenario-tick" }, rows[index]?.time || ""));
  });
  const upper = rows.map((row, index) => [x(index), y(row.p90)]);
  const lower = rows.map((row, index) => [x(index), y(row.p10)]).reverse();
  svg.appendChild(svgNode("path", { d: `${path([...upper, ...lower])} Z`, class: "scenario-band" }));
  ["p10", "p90", "p50"].forEach((key) => svg.appendChild(svgNode("path", { d: path(rows.map((row, index) => [x(index), y(row[key])])), class: `scenario-${key}` })));
  svg.appendChild(svgNode("path", { d: path(values.map((value, index) => [x(index), y(value)])), class: "scenario-actual" }));
  rows.forEach((row, index) => {
    const point = svgNode("circle", { cx: x(index), cy: y(values[index]), r: sources?.[index] === "MANUAL_OVERRIDE" ? 4 : 2.3, class: `scenario-actual-point${sources?.[index] === "MANUAL_OVERRIDE" ? " manual" : ""}${values[index] < row.p10 || values[index] > row.p90 ? " outside-band" : ""}` });
    svg.appendChild(point);
    const hit = svgNode("rect", { x: x(index) - Math.max(7, innerW / rows.length / 2), y: margin.top, width: Math.max(14, innerW / rows.length), height: innerH, class: "scenario-hit", "data-index": index });
    hit.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      svg.setPointerCapture?.(event.pointerId);
      svg.dataset.dragIndex = String(index);
      updateScenarioFromPointer(event, svg, rows, kind);
    });
    const text = `${row.time} · P10 ${Number(row.p10).toFixed(kind === "load" ? 3 : 1)} / P50 ${Number(row.p50).toFixed(kind === "load" ? 3 : 1)} / P90 ${Number(row.p90).toFixed(kind === "load" ? 3 : 1)} · 实际 ${Number(values[index]).toFixed(kind === "load" ? 3 : 1)}`;
    hit.addEventListener("mousemove", (event) => tooltip(event, text));
    hit.addEventListener("mouseleave", hideTooltip);
    svg.appendChild(hit);
  });
  svg.onpointermove = (event) => { if (svg.dataset.dragIndex != null) updateScenarioFromPointer(event, svg, rows, kind); };
  svg.onpointerup = (event) => { delete svg.dataset.dragIndex; svg.releasePointerCapture?.(event.pointerId); hideTooltip(); };
  svg.onpointercancel = () => { delete svg.dataset.dragIndex; };
}

function updateScenarioFromPointer(event, svg, rows, kind) {
  const index = Number(svg.dataset.dragIndex);
  if (!Number.isInteger(index) || index < 0 || index >= rows.length) return;
  const rect = svg.getBoundingClientRect();
  const svgY = (event.clientY - rect.top) / rect.height * 300;
  const margin = { top: 18, right: 20, bottom: 34, left: 58 };
  const innerW = 1100 - margin.left - margin.right;
  const innerH = 300 - margin.top - margin.bottom;
  const domainMin = Number(svg.dataset.domainMin);
  const domainMax = Number(svg.dataset.domainMax);
  const rawValue = domainMax - ((svgY - margin.top) / innerH) * (domainMax - domainMin);
  const value = kind === "load" ? Math.max(0, rawValue) : Math.max(-2000, Math.min(5000, rawValue));
  const values = kind === "load" ? state.draftLoadValues : state.draftPriceValues;
  values[index] = Number(value.toFixed(kind === "load" ? 6 : 3));
  const sources = kind === "load" ? state.draftLoadSources : state.draftPriceSources;
  sources[index] = "MANUAL_OVERRIDE";
  if (kind === "load") state.loadDraftDirty = true;
  else state.priceDraftDirty = true;
  state.scenarioDirty = true;
  const x = (pointIndex) => margin.left + pointIndex / Math.max(rows.length - 1, 1) * innerW;
  const y = (pointValue) => margin.top + (domainMax - Number(pointValue)) / (domainMax - domainMin) * innerH;
  const actualPath = svg.querySelector(".scenario-actual");
  if (actualPath) actualPath.setAttribute("d", path(values.map((pointValue, pointIndex) => [x(pointIndex), y(pointValue)])));
  const point = svg.querySelectorAll(".scenario-actual-point")[index];
  if (point) {
    point.setAttribute("cy", y(values[index]));
    point.setAttribute("class", `scenario-actual-point manual${values[index] < rows[index].p10 || values[index] > rows[index].p90 ? " outside-band" : ""}`);
  }
  const tableInput = document.querySelector(`#${kind === "load" ? "mock-load-table" : "mock-price-table"} tr:nth-child(${index + 1}) input.scenario-value`);
  if (tableInput) { tableInput.value = values[index].toFixed(kind === "load" ? 3 : 1); tableInput.classList.add("manual"); }
  const statusId = kind === "load" ? "load-editor-status" : "price-editor-status";
  document.getElementById(statusId).textContent = scenarioStatus(kind, rows, values, sources);
}

function setScenarioDraft(kind, index, rawValue) {
  const values = kind === "load" ? state.draftLoadValues : state.draftPriceValues;
  const sources = kind === "load" ? state.draftLoadSources : state.draftPriceSources;
  const value = Number(rawValue);
  if (!Number.isFinite(value)) return;
  if (kind === "load" && value < 0) {
    renderMockData(state.data);
    showError(new Error("实际负荷不得为负数"));
    return;
  }
  if (kind === "price" && (value < -2000 || value > 5000)) {
    renderMockData(state.data);
    showError(new Error("实际实时价格必须位于 -2000 到 5000 元/MWh"));
    return;
  }
  values[index] = value;
  sources[index] = "MANUAL_OVERRIDE";
  if (kind === "load") state.loadDraftDirty = true;
  else state.priceDraftDirty = true;
  state.scenarioDirty = true;
  renderMockData(state.data);
}

function renderMockLoadTable(data) {
  const phases = data.load_forecast.phases;
  let phase = phases.find((item) => item.snapshot_id === state.mockPhaseId && item.available);
  if (!phase) { phase = defaultForecastPhase(data); state.mockPhaseId = phase.snapshot_id; }
  const select = document.getElementById("mock-phase-select");
  if (select.options.length !== phases.length) {
    select.replaceChildren();
    phases.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.snapshot_id;
      option.textContent = `${item.label} · ${item.published_at.slice(0, 16).replace("T", " ")}`;
      option.disabled = !item.available;
      select.appendChild(option);
    });
  }
  select.value = phase.snapshot_id;
  const accuracy = phase.quality.wape == null ? "待实现" : `${((1 - phase.quality.wape) * 100).toFixed(2)}%`;
  document.getElementById("mock-phase-summary").textContent = `P50 ${phase.quality.p50_total_mwh.toFixed(3)} MWh · P10-P90总宽度 ${phase.quality.interval_width_mwh.toFixed(3)} MWh · WAPE准确度 ${accuracy}`;
  const truthRows = scenarioRows(data, "load");
  if (!state.draftLoadValues || state.draftLoadValues.length !== truthRows.length) syncScenarioStateFromData(data);
  const rows = phase.rows.map((row, index) => ({
    period: row.period,
    time: row.time,
    p10: Number(row.p10_mwh),
    p50: Number(row.p50_mwh),
    p90: Number(row.p90_mwh),
    actual: Number(truthRows[index]?.actual),
  }));
  const values = state.draftLoadValues || truthRows.map((row) => Number(row.actual));
  const sources = state.draftLoadSources || truthRows.map(() => "SEEDED_RANDOM");
  renderScenarioChart("mock-load-chart", rows, values, sources, "load");
  document.getElementById("load-editor-status").textContent = `${scenarioStatus("load", rows, values, sources)} · 真值基准 D-1`;
  const body = document.getElementById("mock-load-table");
  body.replaceChildren();
  rows.forEach((row, index) => {
    const tr = document.createElement("tr");
    const input = document.createElement("input");
    input.type = "number"; input.step = "0.001"; input.min = "0"; input.value = Number(values[index]).toFixed(3);
    input.className = `scenario-value${sources[index] === "MANUAL_OVERRIDE" ? " manual" : ""}${values[index] < row.p10 || values[index] > row.p90 ? " outside-band" : ""}`;
    input.setAttribute("aria-label", `第${row.period}点实际负荷`);
    input.addEventListener("change", (event) => setScenarioDraft("load", index, event.target.value));
    tr.innerHTML = `<td>${String(row.period).padStart(2, "0")}</td><td>${row.time}</td><td>${Number(row.p10).toFixed(3)}</td><td><b>${Number(row.p50).toFixed(3)}</b></td><td>${Number(row.p90).toFixed(3)}</td>`;
    const valueCell = document.createElement("td"); valueCell.appendChild(input); tr.appendChild(valueCell);
    const status = document.createElement("td"); status.textContent = `${sources[index] === "MANUAL_OVERRIDE" ? "人工" : "随机"}${values[index] < row.p10 || values[index] > row.p90 ? " · 区间外" : ""}`; tr.appendChild(status);
    body.appendChild(tr);
  });
}

function renderMockPriceTable(data) {
  document.getElementById("mock-price-note").textContent = data.price_forecast.scenario_note;
  const rows = scenarioRows(data, "real_time_price");
  if (!state.draftPriceValues || state.draftPriceValues.length !== rows.length) syncScenarioStateFromData(data);
  const values = state.draftPriceValues || rows.map((row) => Number(row.actual));
  const sources = state.draftPriceSources || rows.map(() => "SEEDED_RANDOM");
  renderScenarioChart("mock-price-chart", rows, values, sources, "price");
  document.getElementById("price-editor-status").textContent = scenarioStatus("price", rows, values, sources);
  const body = document.getElementById("mock-price-table"); body.replaceChildren();
  data.price_forecast.rows.forEach((row, index) => {
    const truth = rows[index] || {};
    const tr = document.createElement("tr");
    if (row.scenario === "MIDDAY_PV_SURPLUS") tr.className = "pv-price-row";
    const officialDa = row.official_day_ahead == null ? "—" : Number(row.official_day_ahead).toFixed(3);
    const input = document.createElement("input");
    input.type = "number"; input.step = "0.1"; input.value = Number(values[index]).toFixed(1); input.className = `scenario-value${sources[index] === "MANUAL_OVERRIDE" ? " manual" : ""}${values[index] < truth.p10 || values[index] > truth.p90 ? " outside-band" : ""}`;
    input.setAttribute("aria-label", `第${row.period}点实际实时价格`);
    input.addEventListener("change", (event) => setScenarioDraft("price", index, event.target.value));
    tr.innerHTML = `<td>${String(row.period).padStart(2, "0")}</td><td>${row.time}</td><td>${Number(row.day_ahead_p10).toFixed(1)} / <b>${Number(row.day_ahead_p50).toFixed(1)}</b> / ${Number(row.day_ahead_p90).toFixed(1)}</td><td>${Number(row.real_time_p10).toFixed(1)} / <b>${Number(row.real_time_p50).toFixed(1)}</b> / ${Number(row.real_time_p90).toFixed(1)}</td><td>${officialDa}</td>`;
    const valueCell = document.createElement("td"); valueCell.appendChild(input); tr.appendChild(valueCell);
    const status = document.createElement("td"); status.textContent = `${sources[index] === "MANUAL_OVERRIDE" ? "人工" : "随机"}${values[index] < truth.p10 || values[index] > truth.p90 ? " · 区间外" : ""}`; tr.appendChild(status);
    body.appendChild(tr);
  });
}

function setMockTab(tab) {
  state.mockTab = tab;
  const loadSelected = tab === "load";
  document.getElementById("mock-load-tab").setAttribute("aria-selected", String(loadSelected));
  document.getElementById("mock-price-tab").setAttribute("aria-selected", String(!loadSelected));
  document.getElementById("mock-load-panel").hidden = !loadSelected;
  document.getElementById("mock-price-panel").hidden = loadSelected;
}

function renderMockData(data) {
  syncScenarioStateFromData(data);
  renderMockLoadTable(data);
  renderMockPriceTable(data);
  setMockTab(state.mockTab);
}

function overridesFromDraft(values, sources) {
  if (!values || !sources) return [];
  return values.map((value, index) => sources[index] === "MANUAL_OVERRIDE" ? Number(value) : null);
}

function markSeedDraft(kind, value) {
  const seed = Math.trunc(Number(value));
  if (!Number.isFinite(seed)) return;
  if (kind === "load") {
    state.loadScenarioSeed = seed; state.confirmedLoadOverrides = [];
    state.draftLoadValues = null; state.draftLoadSources = null;
    state.loadDraftDirty = false;
  } else {
    state.priceScenarioSeed = seed; state.confirmedPriceOverrides = [];
    state.draftPriceValues = null; state.draftPriceSources = null;
    state.priceDraftDirty = false;
  }
  state.scenarioDirty = state.loadDraftDirty || state.priceDraftDirty;
  refresh().catch(showError);
}

function randomSeed() {
  return Math.floor(Date.now() % 2147483647);
}

async function confirmScenarioEdits() {
  const previousLoadOverrides = state.confirmedLoadOverrides;
  const previousPriceOverrides = state.confirmedPriceOverrides;
  state.confirmedLoadOverrides = overridesFromDraft(state.draftLoadValues, state.draftLoadSources);
  state.confirmedPriceOverrides = overridesFromDraft(state.draftPriceValues, state.draftPriceSources);
  try {
    await refresh();
  } catch (error) {
    state.confirmedLoadOverrides = previousLoadOverrides;
    state.confirmedPriceOverrides = previousPriceOverrides;
    showError(error);
    return;
  }
  state.scenarioDirty = false;
  state.loadDraftDirty = false;
  state.priceDraftDirty = false;
  state.draftLoadValues = null;
  state.draftPriceValues = null;
  state.draftLoadSources = null;
  state.draftPriceSources = null;
  if (state.data) renderMockData(state.data);
}

function discardScenarioEdits() {
  state.scenarioDirty = false;
  state.loadDraftDirty = false;
  state.priceDraftDirty = false;
  state.draftLoadValues = null;
  state.draftPriceValues = null;
  state.draftLoadSources = null;
  state.draftPriceSources = null;
  if (state.data) renderMockData(state.data);
}

function syncOutputs() {
  document.getElementById("annual-coverage-output").textContent = `${Math.round(state.annualCoverage * 100)}%`;
  document.getElementById("monthly-coverage-output").textContent = `${Math.round(state.monthlyCoverage * 100)}%`;
  document.getElementById("ten-day-coverage-output").textContent = `${Math.round(state.tenDayCoverage * 100)}%`;
  document.getElementById("d3-coverage-output").textContent = `${Math.round(state.d3Coverage * 100)}%`;
  document.getElementById("d2-coverage-output").textContent = `${Math.round(state.d2Coverage * 100)}%`;
  document.getElementById("rolling-min-fill-output").textContent = `${Math.round(state.rollingMinFillRatio * 100)}%`;
  document.getElementById("rolling-max-fill-output").textContent = `${Math.round(state.rollingMaxFillRatio * 100)}%`;
  document.getElementById("risk-output").textContent = state.cvarEnabled ? state.risk.toFixed(2) : "0.00";
  document.getElementById("risk-input").disabled = !state.cvarEnabled;
  document.getElementById("rt-period-output").textContent = `${state.rtPeriod} / 96`;
}

function render(data) {
  state.data = data;
  syncOutputs();
  renderTimeline(data.timeline);
  renderAction(data);
  renderForecast(data);
  renderDecisions(data);
  renderDeclaration(data);
  const storageVisibility = data.meta.storage_visibility || {};
  const storageSection = document.querySelector(".storage-section");
  const storageVisible = Boolean(storageVisibility.visible);
  storageSection.hidden = !storageVisible;
  if (storageVisible) {
    const storageMode = storageVisibility.mode;
    const storageTitles = {
      DAY_AHEAD_CONDITIONAL_PLAN: "3. D-1 储能充放电条件计划",
      REAL_TIME_MPC: "3. 实时储能充放电曲线",
      EXECUTED_HISTORY: "3. 实时储能执行复盘",
    };
    const storageKickers = {
      DAY_AHEAD_CONDITIONAL_PLAN: "DAY-AHEAD STORAGE PLAN",
      REAL_TIME_MPC: "REAL-TIME STORAGE MPC",
      EXECUTED_HISTORY: "STORAGE EXECUTION REVIEW",
    };
    document.getElementById("storage-title").textContent = storageTitles[storageMode] || storageTitles.REAL_TIME_MPC;
    document.getElementById("storage-kicker").textContent = storageKickers[storageMode] || storageKickers.REAL_TIME_MPC;
    document.getElementById("storage-rt-selector").hidden = storageMode !== "REAL_TIME_MPC";
    renderStorage(data);
  } else {
    document.getElementById("storage-rt-selector").hidden = true;
    document.getElementById("storage-chart").replaceChildren();
  }
  if (document.getElementById("mock-data-dialog").open) renderMockData(data);
}

function buildUrl() {
  const url = new URL("/api/simulate", window.location.origin);
  const params = {
    event: state.event, risk: state.risk, cvar_enabled: state.cvarEnabled,
    annual_price: state.annualPrice, monthly_price: state.monthlyPrice, ten_day_price: state.tenDayPrice,
    annual_coverage: state.annualCoverage, monthly_coverage: state.monthlyCoverage,
    ten_day_coverage: state.tenDayCoverage, d3_coverage: state.d3Coverage,
    d2_coverage: state.d2Coverage, target_quantile: state.quantile,
    maximum_adjustment: state.adjustment,
    allow_sell: state.allowSell, rolling_price_edge_lower: state.rollingPriceEdgeLower,
    rolling_price_edge_upper: state.rollingPriceEdgeUpper,
    rolling_min_fill_ratio: state.rollingMinFillRatio,
    rolling_max_fill_ratio: state.rollingMaxFillRatio,
    rt_period: state.rtPeriod, require_milp: true,
    load_scenario_seed: state.loadScenarioSeed,
    price_scenario_seed: state.priceScenarioSeed,
  };
  Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
  const loadOverrides = state.confirmedLoadOverrides;
  const priceOverrides = state.confirmedPriceOverrides;
  if (loadOverrides?.length === 96) url.searchParams.set("actual_load_overrides", JSON.stringify(loadOverrides));
  if (priceOverrides?.length === 96) url.searchParams.set("actual_real_time_price_overrides", JSON.stringify(priceOverrides));
  return url;
}

async function refresh() {
  const requestId = ++activeRequestId;
  beginSolveProgress();
  try {
    const response = await fetch(buildUrl());
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || payload.error || `模拟接口返回 ${response.status}`);
    }
    const payload = await response.json();
    await finishSolveProgress();
    if (requestId !== activeRequestId) return;
    render(payload);
    clearError();
  } catch (error) {
    if (requestId === activeRequestId) {
      cancelSolveProgress();
      showError(error);
    }
    throw error;
  }
}

function bindControls() {
  const bindings = [
    ["annual-price-input", "annualPrice", Number], ["monthly-price-input", "monthlyPrice", Number],
    ["ten-day-price-input", "tenDayPrice", Number],
    ["annual-coverage", "annualCoverage", Number], ["monthly-coverage", "monthlyCoverage", Number],
    ["ten-day-coverage", "tenDayCoverage", Number], ["d3-coverage", "d3Coverage", Number],
    ["d2-coverage", "d2Coverage", Number], ["risk-input", "risk", Number],
    ["quantile-input", "quantile", String],
    ["adjustment-input", "adjustment", Number],
    ["rolling-price-edge-lower-input", "rollingPriceEdgeLower", Number],
    ["rolling-price-edge-upper-input", "rollingPriceEdgeUpper", Number],
    ["rolling-min-fill-input", "rollingMinFillRatio", Number], ["rolling-max-fill-input", "rollingMaxFillRatio", Number],
    ["rt-period-input", "rtPeriod", Number],
  ];
  let timer;
  bindings.forEach(([id, key, cast]) => {
    const input = document.getElementById(id);
    const eventName = input.type === "range" || input.type === "number" ? "input" : "change";
    input.addEventListener(eventName, () => {
      state[key] = cast(input.value);
      syncOutputs();
      clearTimeout(timer);
      timer = setTimeout(() => refresh().catch(showError), 100);
    });
  });
  document.getElementById("cvar-enabled-input").addEventListener("change", (event) => {
    state.cvarEnabled = event.target.checked;
    syncOutputs();
    refresh().catch(showError);
  });
  document.getElementById("allow-sell-input").addEventListener("change", (event) => {
    state.allowSell = event.target.checked;
    refresh().catch(showError);
  });
  const dialog = document.getElementById("mock-data-dialog");
  document.getElementById("open-mock-data").addEventListener("click", () => {
    if (!state.data) return;
    state.mockPhaseId = state.forecastPhaseId || defaultForecastPhase(state.data).snapshot_id;
    renderMockData(state.data);
    dialog.showModal();
  });
  document.getElementById("close-mock-data").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  document.getElementById("mock-load-tab").addEventListener("click", () => setMockTab("load"));
  document.getElementById("mock-price-tab").addEventListener("click", () => setMockTab("price"));
  document.getElementById("mock-phase-select").addEventListener("change", (event) => {
    state.mockPhaseId = event.target.value;
    renderMockLoadTable(state.data);
  });
  document.getElementById("load-scenario-seed").addEventListener("change", (event) => markSeedDraft("load", event.target.value));
  document.getElementById("price-scenario-seed").addEventListener("change", (event) => markSeedDraft("price", event.target.value));
  document.getElementById("randomize-load-scenario").addEventListener("click", () => {
    const seed = randomSeed(); document.getElementById("load-scenario-seed").value = seed; markSeedDraft("load", seed);
  });
  document.getElementById("randomize-price-scenario").addEventListener("click", () => {
    const seed = randomSeed(); document.getElementById("price-scenario-seed").value = seed; markSeedDraft("price", seed);
  });
  document.getElementById("confirm-scenario-edits").addEventListener("click", confirmScenarioEdits);
  document.getElementById("discard-scenario-edits").addEventListener("click", discardScenarioEdits);
  document.getElementById("confirm-scenario-edits-price").addEventListener("click", confirmScenarioEdits);
  document.getElementById("discard-scenario-edits-price").addEventListener("click", discardScenarioEdits);
  document.getElementById("reset-config").addEventListener("click", () => {
    Object.assign(state, defaults);
    state.forecastPhaseId = null;
    state.mockPhaseId = null;
    state.mockTab = "load";
    state.loadScenarioSeed = defaults.loadScenarioSeed;
    state.priceScenarioSeed = defaults.priceScenarioSeed;
    state.confirmedLoadOverrides = [];
    state.confirmedPriceOverrides = [];
    state.draftLoadValues = null;
    state.draftPriceValues = null;
    state.draftLoadSources = null;
    state.draftPriceSources = null;
    state.scenarioDirty = false;
    state.loadDraftDirty = false;
    state.priceDraftDirty = false;
    ["annual-price-input", "monthly-price-input", "ten-day-price-input", "annual-coverage", "monthly-coverage", "ten-day-coverage", "d3-coverage", "d2-coverage", "risk-input", "quantile-input", "adjustment-input", "rolling-price-edge-lower-input", "rolling-price-edge-upper-input", "rolling-min-fill-input", "rolling-max-fill-input", "rt-period-input"].forEach((id) => { document.getElementById(id).value = defaults[{ "annual-price-input": "annualPrice", "monthly-price-input": "monthlyPrice", "ten-day-price-input": "tenDayPrice", "annual-coverage": "annualCoverage", "monthly-coverage": "monthlyCoverage", "ten-day-coverage": "tenDayCoverage", "d3-coverage": "d3Coverage", "d2-coverage": "d2Coverage", "risk-input": "risk", "quantile-input": "quantile", "adjustment-input": "adjustment", "rolling-price-edge-lower-input": "rollingPriceEdgeLower", "rolling-price-edge-upper-input": "rollingPriceEdgeUpper", "rolling-min-fill-input": "rollingMinFillRatio", "rolling-max-fill-input": "rollingMaxFillRatio", "rt-period-input": "rtPeriod" }[id]]; });
    document.getElementById("allow-sell-input").checked = defaults.allowSell;
    document.getElementById("cvar-enabled-input").checked = defaults.cvarEnabled;
    refresh().catch(showError);
  });
}

function showError(error) {
  let banner = document.querySelector("#error-banner");
  if (!banner) { banner = document.createElement("p"); banner.id = "error-banner"; banner.className = "error-banner"; document.querySelector("main").prepend(banner); }
  banner.textContent = `页面加载失败：${error.message}`;
}

function clearError() {
  document.querySelector("#error-banner")?.remove();
}

async function initialize() {
  bindControls();
  await refresh();
}

initialize().catch(showError);
