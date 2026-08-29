import { useState } from "react";
import { useProfile, useImportResume } from "../api/hooks";
import { Loading, ErrorBanner } from "../components/Feedback";

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
