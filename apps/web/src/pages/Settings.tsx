import { useEffect, useMemo, useState } from "react";
import { useSources, useToggleSource, useSettings, useBackendHealth } from "../api/hooks";
import { Loading, ErrorBanner } from "../components/Feedback";
import { HealthDot } from "../components/HealthDot";
import { useUpdateStrategy } from "../api/hooks";
import { buildOptionsBody, formatOptionValue, readOptionFields } from "../lib/options";
import type { JsonObject, SourceInfo } from "../api/types";

export function Settings() {
  const { data: sources, isLoading: srcLoading } = useSources();
  const { data: settings } = useSettings();
  const { data: backends } = useBackendHealth();
  const toggleSrc = useToggleSource();
  const strategyMut = useUpdateStrategy();
  if (srcLoading) return <Loading />;
  const configurable = (sources ?? []).filter((src) => readOptionFields(src.options_schema).length > 0);
  return (
    <div>
      <h1 style={{ marginBottom: 24 }}>Settings</h1>

      {toggleSrc.isError && (
        <ErrorBanner message={toggleSrc.error instanceof Error ? toggleSrc.error.message : String(toggleSrc.error)} />
      )}

      {settings && (
        <div className="card">
          <h3>General</h3>
          <p>Execution mode: <strong>{settings.execution_mode}</strong></p>
          <p>LLM enabled: <strong>{String(settings.llm_enabled)}</strong>{settings.default_provider ? ` (${settings.default_provider})` : ""}</p>
          <p>Browser preference: {settings.browser_preferred.join(", ") || "(none)"}</p>
        </div>
      )}

      {settings?.providers && settings.providers.length > 0 && (
        <div className="card">
          <h3>LLM Providers</h3>
          <table><thead><tr><th>Name</th><th>Kind</th><th>Model</th><th>API Key</th><th>Health</th></tr></thead>
          <tbody>
            {settings.providers.map((p) => {
              const h = backends?.llm.find((r) => r.name === p.name);
              return <tr key={p.name}>
                <td>{p.name}</td><td>{p.kind}</td><td>{p.default_model ?? "—"}</td>
                <td>{p.has_api_key ? "✓" : "—"}</td>
                <td>{h ? <HealthDot state={h.state} /> : "—"}</td>
              </tr>;
            })}
          </tbody></table>
        </div>
      )}

      <div className="card">
        <h3>Job Sources</h3>
        <table><thead><tr><th>Slug</th><th>Name</th><th>Tier</th><th>Enabled</th><th>Health</th></tr></thead>
        <tbody>
          {(sources ?? []).map((src) => (
            <tr key={src.slug}>
              <td>{src.slug}</td><td>{src.name}</td><td>{src.tier}</td>
              <td>
                <button className="secondary" onClick={() => toggleSrc.mutate({ slug: src.slug, enabled: !src.enabled })}>
                  {src.enabled ? "Disable" : "Enable"}
                </button>
              </td>
              <td>{src.health ? <HealthDot state={src.health.state} /> : "—"}</td>
            </tr>
          ))}
        </tbody></table>
      </div>

      {/*
        One form per source that publishes an `options_schema`. The fields come
        from the plugin itself, so a new source — or a new option on an existing
        one — appears here with no change to this file.
      */}
      {configurable.map((src) => (
        <div className="card" key={src.slug}>
          <h3>{src.name} options</h3>
          <SourceOptions
            source={src}
            saving={toggleSrc.isPending}
            onSave={(options) =>
              toggleSrc.mutate({ slug: src.slug, enabled: src.enabled, options })
            }
          />
        </div>
      ))}

      {backends && (
        <div className="card">
          <h3>Browser & Agent Backends</h3>
          <table><thead><tr><th>Kind</th><th>Name</th><th>State</th><th>Detail</th></tr></thead>
          <tbody>
            {[...backends.browsers, ...backends.agents, ...backends.email].map((b, i) => (
              <tr key={i}><td>{b.kind}</td><td>{b.name}</td><td><HealthDot state={b.state} /></td><td>{b.detail?.slice(0, 60)}</td></tr>
            ))}
          </tbody></table>
        </div>
      )}

      {settings?.strategy && (
        <div className="card">
          <h3>Search Strategy</h3>
          <p style={{ marginBottom: 12, color: "var(--text-muted)" }}>Exact numeric values are stored, not vague labels.</p>
          <StrategySlider label="Depth bias" value={settings.strategy.depth_bias} onChange={(v) => strategyMut.mutate({ strategy: { ...settings.strategy, depth_bias: v } })} />
          <StrategySlider label="Application volume" value={settings.strategy.application_volume_bias} onChange={(v) => strategyMut.mutate({ strategy: { ...settings.strategy, application_volume_bias: v } })} />
          <StrategySlider label="Title exploration" value={settings.strategy.title_exploration} onChange={(v) => strategyMut.mutate({ strategy: { ...settings.strategy, title_exploration: v } })} />
          <StrategySlider label="Minimum fit score" value={settings.strategy.minimum_fit_score} min={0} max={1} step={0.05} onChange={(v) => strategyMut.mutate({ strategy: { ...settings.strategy, minimum_fit_score: v } })} />
        </div>
      )}
    </div>
  );
}

/**
 * The editable half of one source: a control per schema property, and a save
 * that hands the whole options object back to the server.
 *
 * Options can only be written through the enable endpoint, so a disabled
 * source shows its fields read-only with a note rather than a save button that
 * would silently re-enable it.
 */
function SourceOptions({ source, onSave, saving }: {
  source: SourceInfo;
  onSave: (options: JsonObject) => void;
  saving: boolean;
}) {
  const fields = useMemo(() => readOptionFields(source.options_schema), [source.options_schema]);
  const [draft, setDraft] = useState<Record<string, string>>({});

  useEffect(() => {
    setDraft(
      Object.fromEntries(
        fields.map((field) => [field.name, formatOptionValue(field, source.options[field.name])]),
      ),
    );
    // Re-seeded whenever the server reports different options for this source.
  }, [fields, source.options]);

  if (!source.enabled) {
    return (
      <div>
        {fields.map((field) => (
          <div key={field.name} style={{ marginBottom: 12 }}>
            <label htmlFor={`${source.slug}-${field.name}`}>{field.label}</label>
            <input
              id={`${source.slug}-${field.name}`}
              value={draft[field.name] ?? ""}
              disabled
              onChange={(e) => setDraft({ ...draft, [field.name]: e.target.value })}
            />
          </div>
        ))}
        <p style={{ color: "var(--text-muted)" }}>Enable this source to configure its options.</p>
      </div>
    );
  }

  return (
    <div>
      {fields.map((field) => (
        <div key={field.name} style={{ marginBottom: 12 }}>
          <label htmlFor={`${source.slug}-${field.name}`}>{field.label}</label>
          <input
            id={`${source.slug}-${field.name}`}
            value={draft[field.name] ?? ""}
            placeholder={field.kind === "list" ? "comma-separated" : undefined}
            disabled={saving}
            onChange={(e) => setDraft({ ...draft, [field.name]: e.target.value })}
          />
          {field.description && (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{field.description}</div>
          )}
        </div>
      ))}
      <button
        disabled={saving}
        onClick={() => onSave(buildOptionsBody(fields, draft, source.options))}
      >
        Save options
      </button>
    </div>
  );
}

function StrategySlider({ label, value, onChange, min = 0, max = 1, step = 0.01 }: {
  label: string; value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number;
}) {
  return (
    <div style={{ marginBottom: 12 }}>
      <label>{label}</label>
      <div className="slider-group">
        <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(parseFloat(e.target.value))} />
        <input type="number" min={min} max={max} step={step} value={value} onChange={(e) => onChange(parseFloat(e.target.value))} />
      </div>
    </div>
  );
}
