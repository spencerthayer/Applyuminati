import { useState } from "react";
import { useProfile, useImportResume, useUpdatePreferences } from "../api/hooks";
import { Loading, ErrorBanner } from "../components/Feedback";
import { isRecord, readNumber, readString, readStringArray } from "../lib/guards";
import { parseListInput } from "../lib/options";
import {
  EMPLOYMENT_TYPES,
  REMOTE_MODES,
  SENIORITY_LEVELS,
  type EmploymentType,
  type ProfileResponse,
  type RemoteMode,
  type SeniorityLevel,
} from "../api/types";

export function Profile() {
  const { data: profile, isLoading } = useProfile();
  const importMut = useImportResume();
  const [text, setText] = useState("");
  if (isLoading) return <Loading />;
  return (
    <div>
      <h1 style={{ marginBottom: 24 }}>Profile</h1>
      {profile ? (
        <>
          <div className="card">
            <h3>{profile.name ?? "Unnamed"}</h3>
            <p style={{ color: "var(--text-muted)" }}>{profile.headline ?? ""} · {profile.email ?? ""}</p>
            <div className="grid grid-4" style={{ marginTop: 12 }}>
              {Object.entries(profile.counts).map(([key, val]) => (
                <div key={key} className="stat"><div className="num">{val}</div><div className="label">{key}</div></div>
              ))}
            </div>
          </div>
          {Object.entries(profile.claim_levels).length > 0 && (
            <div className="card">
              <h3>Claim Levels</h3>
              <table><tbody>
                {Object.entries(profile.claim_levels).map(([level, count]) => (
                  <tr key={level}><td>{level}</td><td>{count}</td></tr>
                ))}
              </tbody></table>
            </div>
          )}
          <PreferencesEditor profile={profile} />
        </>
      ) : (
        <div className="card">
          <h3>Import a JSON Resume</h3>
          <p style={{ marginBottom: 12, color: "var(--text-muted)" }}>Paste your resume.json below or upload a file.</p>
          <textarea rows={12} value={text} onChange={(e) => setText(e.target.value)} placeholder='{"basics": {"name": "Your Name", ...}, ...}' />
          <div style={{ marginTop: 12, display: "flex", gap: 12 }}>
            <input type="file" accept=".json" onChange={(e) => {
              const file = e.target.files?.[0]; if (!file) return;
              const reader = new FileReader();
              reader.onload = () => setText(String(reader.result));
              reader.readAsText(file);
            }} />
            <button disabled={!text} onClick={() => {
              try { importMut.mutate({ resume: JSON.parse(text), replace: true }); }
              catch { /* error shown by hook */ }
            }}>Import</button>
          </div>
          {importMut.isError && <ErrorBanner message="Import failed" />}
        </div>
      )}
    </div>
  );
}

/**
 * The job-seeking preferences the API exposes as
 * `PUT /profile/preferences`.
 *
 * Every field is seeded from the profile's stored targets and every field is
 * sent on save; the server treats an omitted field as "leave unchanged", so
 * sending the whole set is safe and makes the saved body self-describing.
 */
function PreferencesEditor({ profile }: { profile: ProfileResponse }) {
  const save = useUpdatePreferences();
  const targets = isRecord(profile.targets) ? profile.targets : {};
  const [titles, setTitles] = useState(readStringArray(targets, "titles").join(", "));
  const [locations, setLocations] = useState(readLocationRaws(targets).join(", "));
  const [seniority, setSeniority] = useState(readString(targets, "seniority"));
  const [remoteModes, setRemoteModes] = useState<RemoteMode[]>(readEnumList(targets, "remote_modes"));
  const [employmentTypes, setEmploymentTypes] = useState<EmploymentType[]>(
    readEnumList(targets, "employment_types"),
  );
  const [minimum, setMinimum] = useState(
    readCompensation(targets).minimum === null ? "" : String(readCompensation(targets).minimum),
  );
  const [currency, setCurrency] = useState(readCompensation(targets).currency);

  return (
    <div className="card">
      <h3>Job Preferences</h3>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="pref-titles">Target titles</label>
        <input id="pref-titles" value={titles} onChange={(e) => setTitles(e.target.value)} />
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Comma-separated. These become the search queries.</div>
      </div>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="pref-locations">Locations</label>
        <input id="pref-locations" value={locations} onChange={(e) => setLocations(e.target.value)} />
      </div>
      <div style={{ marginBottom: 12 }}>
        <label htmlFor="pref-seniority">Seniority</label>
        <select id="pref-seniority" value={seniority} onChange={(e) => setSeniority(e.target.value)}>
          <option value="">Not set</option>
          {SENIORITY_LEVELS.map((level) => (
            <option key={level} value={level}>{level}</option>
          ))}
        </select>
      </div>
      <fieldset style={{ marginBottom: 12 }}>
        <legend>Remote modes</legend>
        {REMOTE_MODES.map((mode) => (
          <label key={mode} style={{ marginRight: 12 }}>
            <input
              type="checkbox"
              checked={remoteModes.includes(mode)}
              onChange={(e) =>
                setRemoteModes(
                  e.target.checked
                    ? [...remoteModes, mode]
                    : remoteModes.filter((value) => value !== mode),
                )
              }
            />
            {mode}
          </label>
        ))}
      </fieldset>
      <fieldset style={{ marginBottom: 12 }}>
        <legend>Employment types</legend>
        {EMPLOYMENT_TYPES.map((type) => (
          <label key={type} style={{ marginRight: 12 }}>
            <input
              type="checkbox"
              checked={employmentTypes.includes(type)}
              onChange={(e) =>
                setEmploymentTypes(
                  e.target.checked
                    ? [...employmentTypes, type]
                    : employmentTypes.filter((value) => value !== type),
                )
              }
            />
            {type}
          </label>
        ))}
      </fieldset>
      <div style={{ display: "flex", gap: 12, marginBottom: 12 }}>
        <div>
          <label htmlFor="pref-minimum">Minimum compensation</label>
          <input
            id="pref-minimum"
            type="number"
            value={minimum}
            onChange={(e) => setMinimum(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="pref-currency">Compensation currency</label>
          <input id="pref-currency" value={currency} onChange={(e) => setCurrency(e.target.value)} />
        </div>
      </div>
      <button
        disabled={save.isPending}
        onClick={() =>
          save.mutate({
            titles: parseListInput(titles),
            locations: parseListInput(locations),
            seniority: (seniority || null) as SeniorityLevel | null,
            remote_modes: remoteModes,
            employment_types: employmentTypes,
            minimum_compensation: minimum === "" ? null : Number(minimum),
            compensation_currency: currency || null,
          })
        }
      >
        {save.isPending ? "Saving…" : "Save preferences"}
      </button>
      {save.isError && (
        <ErrorBanner message={save.error instanceof Error ? save.error.message : String(save.error)} />
      )}
    </div>
  );
}

/** `targets.locations` is a list of `Location` objects; the API takes their raw strings. */
function readLocationRaws(targets: Record<string, unknown>): string[] {
  const locations = targets.locations;
  if (!Array.isArray(locations)) return [];
  return locations
    .map((entry) => (isRecord(entry) ? readString(entry, "raw") : ""))
    .filter((raw) => raw !== "");
}

function readEnumList<T extends string>(targets: Record<string, unknown>, key: string): T[] {
  return readStringArray(targets, key) as T[];
}

function readCompensation(targets: Record<string, unknown>): { minimum: number | null; currency: string } {
  const floor = targets.compensation_floor;
  if (!isRecord(floor)) return { minimum: null, currency: "" };
  return {
    minimum: floor.minimum === undefined || floor.minimum === null ? null : readNumber(floor, "minimum", 0),
    currency: readString(floor, "currency"),
  };
}
