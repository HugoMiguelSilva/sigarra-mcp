const cardsEl = document.getElementById("admin-cards");
const timelineTableEl = document.getElementById("timeline-table");
const convTimelineTableEl = document.getElementById("conversations-timeline-table");
const toolsTableEl = document.getElementById("tools-table");
const unsupportedListEl = document.getElementById("unsupported-list");
const dislikedListEl = document.getElementById("disliked-list");
const granularitySelect = document.getElementById("granularity-select");
const refreshBtn = document.getElementById("refresh-admin");

function fmtMs(v) {
  return `${Number(v || 0).toFixed(2)} ms`;
}

function fmtMB(v) {
  return `${Number(v || 0).toFixed(2)} MB`;
}

function renderCards(stats) {
  const totals = stats.totals || {};
  const perf = stats.performance || {};
  const storage = stats.storage || {};

  const cards = [
    { label: "Conversas", value: totals.conversations ?? 0 },
    { label: "Perguntas", value: totals.questions ?? 0 },
    { label: "Respostas", value: totals.answers ?? 0 },
    { label: "Utilizadores", value: totals.users ?? 0 },
    { label: "Tempo médio", value: fmtMs(perf.avg_response_ms) },
    { label: "P95", value: fmtMs(perf.p95_response_ms) },
    { label: "Máximo", value: fmtMs(perf.max_response_ms) },
    { label: "BD ocupada", value: fmtMB(storage.db_size_mb) },
  ];

  cardsEl.innerHTML = cards
    .map(
      (card) => `
      <div class="admin-card">
        <p>${card.label}</p>
        <h3>${card.value}</h3>
      </div>`
    )
    .join("");
}

function renderSimpleTable(targetEl, headers, rows) {
  if (!rows.length) {
    targetEl.innerHTML = "<p>Sem dados.</p>";
    return;
  }

  const head = headers.map((h) => `<th>${h}</th>`).join("");
  const body = rows
    .map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`)
    .join("");

  targetEl.innerHTML = `
    <table class="admin-table">
      <thead><tr>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderQuestionList(targetEl, data) {
  if (!data.length) {
    targetEl.innerHTML = "<p>Sem registos.</p>";
    return;
  }

  targetEl.innerHTML = data
    .map(
      (item) => `
      <article class="admin-log-item">
        <p><strong>Pergunta:</strong> ${item.question}</p>
        <p><strong>Resposta:</strong> ${item.answer}</p>
        <p class="muted">${item.created_at}</p>
      </article>
    `
    )
    .join("");
}

async function loadAdminStats() {
  const granularity = granularitySelect.value;
  try {
    const response = await fetch(`/api/admin/stats?granularity=${encodeURIComponent(granularity)}`, {
      cache: "no-store",
    });
    const stats = await response.json();

    if (!response.ok) {
      cardsEl.innerHTML = `<p>${stats.detail || "Erro ao carregar métricas."}</p>`;
      return;
    }

    renderCards(stats);

    renderSimpleTable(
      timelineTableEl,
      ["Período", "Perguntas", "Respostas"],
      (stats.timeline || []).map((row) => [row.bucket, row.questions, row.answers])
    );

    renderSimpleTable(
      convTimelineTableEl,
      ["Período", "Conversas"],
      (stats.conversations_timeline || []).map((row) => [row.bucket, row.conversations])
    );

    renderSimpleTable(
      toolsTableEl,
      ["Tool", "Utilizações"],
      (stats.tools_used || []).map((row) => [row.tool, row.count])
    );

    renderQuestionList(unsupportedListEl, stats.unsupported_questions || []);
    renderQuestionList(dislikedListEl, stats.disliked_answers || []);
  } catch (_) {
    cardsEl.innerHTML = "<p>Erro de rede ao carregar dashboard.</p>";
  }
}

refreshBtn.addEventListener("click", () => {
  loadAdminStats();
});

granularitySelect.addEventListener("change", () => {
  loadAdminStats();
});

loadAdminStats();
