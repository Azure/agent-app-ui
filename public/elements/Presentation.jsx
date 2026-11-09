import { useRef, useState } from "react";

const exact = (value) => value === null ? "—" : String(value);
const seriesColor = index => `var(--plot-${index % 8 + 1})`;
const status = value => <span className="presentation-status" data-status={value}>{value}</span>;

// Geometry alone is approximate. Text, sorting and copy keep source strings.
function parts(value) {
  const text = String(value), negative = text.startsWith("-");
  const [whole, fraction = ""] = text.replace(/^-/, "").split(".");
  const significant = whole.replace(/^0+/, "");
  const first = fraction.search(/[1-9]/);
  const digits = significant ? significant + fraction : first < 0 ? "0" : fraction.slice(first);
  const exponent = significant ? significant.length - 1 : first < 0 ? 0 : -first - 1;
  const coefficient = Number(digits.slice(0, 15)) / (10 ** (Math.min(15, digits.length) - 1));
  return { coefficient: (negative ? -1 : 1) * coefficient, exponent };
}

function compareDecimal(a, b) {
  function canonical(value) {
    const negative = String(value).startsWith("-");
    const [whole, fraction = ""] = String(value).replace(/^-/, "").split(".");
    const integer = whole.replace(/^0+/, "") || "0";
    return { negative: negative && /[1-9]/.test(integer + fraction), integer, fraction };
  }
  const left = canonical(a), right = canonical(b);
  if (left.negative !== right.negative) return left.negative ? -1 : 1;
  const width = Math.max(left.fraction.length, right.fraction.length);
  const l = left.integer + left.fraction.padEnd(width, "0");
  const r = right.integer + right.fraction.padEnd(width, "0");
  const comparison = left.integer.length - right.integer.length || (l < r ? -1 : l > r ? 1 : 0);
  return left.negative ? -comparison : comparison;
}

function Table({ item, follow, busy }) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState(null);
  const [copied, setCopied] = useState("");
  const hasStatus = item.rows.some(row => row.status);
  const visibleCells = row => [
    ...item.columns.map(c => exact(row.values[c.id])),
    ...(hasStatus ? [row.status?.value || "—"] : []),
    ...(item.rowAction ? [row.entityId && row.followUp ? item.rowAction.label : ""] : []),
  ];
  const rows = item.rows.filter(row => visibleCells(row).some(value => value.toLocaleLowerCase().includes(query.toLocaleLowerCase())));
  if (sort) {
    const column = item.columns.find(c => c.id === sort.id);
    rows.sort((a, b) => {
      const l = a.values[column.id], r = b.values[column.id];
      if (l === null || r === null) return l === r ? 0 : l === null ? 1 : -1;
      const order = ["integer", "decimal"].includes(column.type) ? compareDecimal(l, r)
        : String(l) < String(r) ? -1 : String(l) > String(r) ? 1 : 0;
      return order * sort.direction;
    });
  }
  async function copyRows() {
    const headings = item.columns.map(c => `${c.label}${c.unit ? ` (${c.unit})` : ""}`);
    if (hasStatus) headings.push("Status");
    if (item.rowAction) headings.push("Follow-up");
    const cells = rows.map(visibleCells);
    const tsv = [headings, ...cells].map(row => row.map(cell => /[\t\n"]/.test(cell)
      ? `"${cell.replaceAll('"', '""')}"` : cell).join("\t")).join("\n");
    try { await navigator.clipboard.writeText(tsv); setCopied("Visible rows copied."); }
    catch { setCopied("Clipboard unavailable. Select the exact table values to copy."); }
  }
  return <div>
    <div className="presentation-toolbar">
    <label>Filter displayed values <input value={query} onChange={event => setQuery(event.target.value)} /></label>
    <button onClick={copyRows}>Copy visible rows</button></div><p className="presentation-copy-status" role="status">{copied}</p>
    <div className="presentation-scroll" tabIndex={0} role="region" aria-label="Scrollable data table">
    <table><thead><tr>
      {item.columns.map(c => <th key={c.id} scope="col"
        aria-sort={sort?.id === c.id ? sort.direction === 1 ? "ascending" : "descending" : "none"}>
        <button onClick={() => setSort({ id: c.id, direction: sort?.id === c.id ? -sort.direction : 1 })}
          aria-label={`Sort by ${c.label}`}>{c.label} {c.unit || ""}</button></th>)}
      {hasStatus && <th scope="col">Status</th>}
      {item.rowAction && <th scope="col">Follow-up</th>}
    </tr></thead><tbody>{rows.map(row => <tr key={row.id}>
      {item.columns.map(c => <td key={c.id}>{exact(row.values[c.id])}</td>)}
      {hasStatus && <td>{row.status ? status(row.status.value) : "—"}</td>}
      {item.rowAction && <td>{row.entityId && row.followUp && <button disabled={busy}
        onClick={() => follow(row.followUp.prompt)}>{item.rowAction.label}</button>}</td>}
    </tr>)}</tbody></table></div>
  </div>;
}

export default function Presentation() {
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  async function follow(prompt) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    try { await sendUserMessage(prompt); }
    finally { sending.current = false; setBusy(false); }
  }
  function block(item) {
    if (item.type === "cards") return <dl className="presentation-cards">
      {item.cards.map((card, i) => <div key={i} className="presentation-card">
        <dt>{card.label}</dt><dd className="presentation-value">{exact(card.value.value)} <span className="presentation-unit">{card.unit || ""}</span></dd>
        {card.status && <dd>Status: {status(card.status.value)}</dd>}
        {card.trend && <dd className="presentation-trend">Trend: {card.trend.value}</dd>}
      </div>)}
    </dl>;
    if (item.type === "table") return <Table item={item} follow={follow} busy={busy} />;
    if (item.type === "suggestions") return <div className="presentation-suggestions">
      {item.items.map((p, i) => <button key={i} disabled={busy}
        onClick={() => follow(p.prompt)}>{p.prompt}</button>)}
    </div>;
    if (item.type === "chart") {
      const partsBySeries = item.series.map(s => s.values.map(v => v === null ? null : parts(v)));
      const largest = Math.max(...partsBySeries.flat().filter(v => v !== null && v.coefficient !== 0).map(v => v.exponent));
      const exponent = Number.isFinite(largest) ? largest : 0;
      const numbers = partsBySeries.map(s => s.map(v => v === null ? null : v.coefficient * 10 ** (v.exponent - exponent)));
      const values = numbers.flat().filter(v => v !== null);
      const low = Math.min(0, ...values), high = Math.max(0, ...values);
      const range = high - low || 1;
      const yFor = value => 190 - (value - low) / range * 150;
      const baseline = yFor(0);
      const slot = 480 / item.categories.length;
      const points = numbers.map(s => s.map((v, i) => v === null ? null
        : [80 + (i + 0.5) * slot, yFor(v)]));
      // Axis values are approximate; labels/tooltips/table retain source decimals.
      const tickLabel = value => {
        const scaled = value * 10 ** exponent;
        return Number.isFinite(scaled) && (scaled === 0 || Math.abs(scaled) >= .001 && Math.abs(scaled) < 1e7)
          ? Number(scaled.toPrecision(4)).toString()
          : `${Number(value.toPrecision(3))} × 10^${exponent}`;
      };
      return <div><div className="presentation-legend">{item.series.map((s, si) =>
        <span key={si} style={{"--series-color": seriesColor(si)}}>{s.label}{s.unit ? ` (${s.unit})` : ""}</span>
      )}</div><div className="presentation-scroll" tabIndex={0} role="region" aria-label="Scrollable chart">
      <svg viewBox="0 0 600 240" role="img" aria-label={item.title || "Chart"} className="presentation-chart">
        {[0, .5, 1].map((fraction, i) => {
          const value = low + range * fraction, y = yFor(value);
          return <g key={i}><line className="chart-grid" x1="80" y1={y} x2="560" y2={y} />
            <text x="70" y={y + 4} textAnchor="end">{tickLabel(value)}</text></g>;
        })}
        <line className="chart-baseline" x1="80" y1={baseline} x2="560" y2={baseline} />
        {item.categories.map((c, i) => i % Math.ceil(item.categories.length / 8) === 0 && <text key={i} x={80 + (i + .5) * slot} y="218" textAnchor="middle">
          <title>{c.label}</title>{c.label.length > 16 ? c.label.slice(0, 14) + "…" : c.label}
        </text>)}
        {item.chartType === "line" && points.map((series, si) => {
          const paths = []; let current = [];
          series.forEach(point => {
            if (point) current.push(point.join(","));
            else if (current.length) { paths.push(current); current = []; }
          });
          if (current.length) paths.push(current);
          return paths.map((path, i) => <polyline key={`${si}-${i}`} points={path.join(" ")}
            fill="none" stroke={seriesColor(si)} strokeWidth="2.5" strokeDasharray={si ? `${si + 2} 3` : undefined} />);
        })}
        {item.series.map((s, si) => s.values.map((v, i) => {
          if (v === null) return null;
          const [x, y] = points[si][i];
          if (!Number.isFinite(y)) return null;
          return <g key={`${si}-${i}`}>{item.chartType === "line"
            ? <circle cx={x} cy={y} r={3 + si * 0.4} fill={seriesColor(si)}>
                <title>{item.categories[i].label}: {s.label} {v} {s.unit || ""}</title></circle>
            : <rect x={x - slot * .32 + si * slot * .64 / item.series.length}
                y={Math.min(y, baseline)} width={slot * .6 / item.series.length}
                height={Math.abs(baseline-y)} fill={seriesColor(si)} rx="2"><title>
                  {item.categories[i].label}: {s.label} {v} {s.unit || ""}</title></rect>}
            {item.categories.length <= 6 && item.series.length <= 2 && <text className="chart-value"
              x={item.chartType === "line" ? x : x - slot * .32 + (si + .5) * slot * .64 / item.series.length}
              y={y + (numbers[si][i] < 0 ? 16 : -8)} textAnchor="middle">{v}</text>}
          </g>;
        }))}
      </svg></div><div className="presentation-scroll" tabIndex={0} role="region" aria-label="Exact chart values"><table><caption>Exact chart values</caption><tbody>
        {item.categories.map((c, i) => <tr key={c.sourceRowId}><th scope="row">{c.label}</th>
          {item.series.map((s, si) => <td key={si} tabIndex={0}>{s.label}: {exact(s.values[i])} {s.unit || ""}</td>)}
        </tr>)}
      </tbody></table></div></div>;
    }
    return null;
  }
  return <div className="inline-presentation">
    {(props.contributions || []).map((c, i) => <section key={i} aria-label={c.agentId}>
      <p className="presentation-source presentation-agent">{c.agentId}</p>
      {!!c.sourceRefs?.length && <p className="presentation-source">
        Sources: {c.sourceRefs.map(s => s.toolName).join(", ")}
      </p>}
      {c.blocks.map(item => <section key={item.blockId} className="presentation-block">
        {item.title && <h3>{item.title}</h3>}{block(item)}
      </section>)}
    </section>)}
    <details><summary>Text alternative</summary><pre>{props.markdown}</pre></details>
  </div>;
}
