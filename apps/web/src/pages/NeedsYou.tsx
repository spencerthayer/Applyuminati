import { useState } from "react";

import { useInbox, useOpenBrowser, useResolveInbox } from "../api/hooks";
import type { HostPresence, InboxEntry, InterventionResolution } from "../api/types";
import { Loading } from "../components/Feedback";

function actionLabel(entry: InboxEntry): string {
  if (entry.requires_browser_handoff) return "Needs browser";
  if (entry.question_text) return "Answer";
  return "Review";
}

function hostPresenceLabel(presence: HostPresence): string {
  switch (presence) {
    case "connected":
      return "Mac connected";
    case "offline":
      return "Mac offline";
    case "session_unavailable":
      return "session unavailable";
    case "not_required":
      return "in-app";
    default: {
      const exhaustive: never = presence;
      return exhaustive;
    }
  }
}

function hostPresenceClass(presence: HostPresence): string {
  switch (presence) {
    case "connected":
      return "badge badge-green";
    case "offline":
      return "badge badge-red";
    case "session_unavailable":
      return "badge badge-yellow";
    case "not_required":
      return "badge badge-muted";
    default: {
      const exhaustive: never = presence;
      return exhaustive;
    }
  }
}

export function NeedsYou() {
  const { data: items, isLoading } = useInbox();
  const resolve = useResolveInbox();
  const openBrowser = useOpenBrowser();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [handoffDetail, setHandoffDetail] = useState<Record<string, string>>({});

  if (isLoading || !items) return <Loading />;

  const act = (entry: InboxEntry, resolution: InterventionResolution, payload?: Record<string, string>) => {
    resolve.mutate({
      attemptId: entry.attempt_id,
      interventionId: entry.intervention_id,
      body: { resolution, payload },
    });
  };

  const openHost = (entry: InboxEntry) => {
    openBrowser.mutate(entry.attempt_id, {
      onSuccess: (result) => {
        setHandoffDetail((current) => ({
          ...current,
          [entry.intervention_id]: result.detail,
        }));
      },
      onError: (error) => {
        setHandoffDetail((current) => ({
          ...current,
          [entry.intervention_id]: error.message,
        }));
      },
    });
  };

  return (
    <div>
      <h1 style={{ marginBottom: 8 }}>Needs you</h1>
      <p style={{ color: "var(--text-muted)", marginBottom: 24 }}>
        {items.length} waiting. Automation paused here on purpose.
      </p>
      {items.length === 0 ? (
        <div className="empty">
          <h3>Nothing needs you</h3>
          <p>Applications in progress will appear here when a site requires a human.</p>
        </div>
      ) : (
        items.map((entry) => {
          const presence = entry.host_presence ?? "not_required";
          return (
            <div className="card" key={entry.intervention_id}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                <div>
                  <strong>
                    {entry.company ?? "Unknown company"}
                    {entry.title ? ` | ${entry.title}` : ""}
                  </strong>
                  <div style={{ color: "var(--text-muted)", marginTop: 4 }}>{entry.instruction}</div>
                  {entry.task_space_id ? (
                    <div className="provenance" style={{ marginTop: 8 }}>
                      Browser task: {entry.task_space_id}
                    </div>
                  ) : null}
                  {handoffDetail[entry.intervention_id] ? (
                    <div className="provenance" style={{ marginTop: 8 }}>
                      {handoffDetail[entry.intervention_id]}
                    </div>
                  ) : null}
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-end" }}>
                  <span className="badge badge-yellow">{actionLabel(entry)}</span>
                  <span className={hostPresenceClass(presence)}>{hostPresenceLabel(presence)}</span>
                </div>
              </div>
              {entry.question_text ? (
                <div style={{ marginTop: 12 }}>
                  <label htmlFor={`answer-${entry.intervention_id}`}>{entry.question_text}</label>
                  <input
                    id={`answer-${entry.intervention_id}`}
                    value={answers[entry.intervention_id] ?? ""}
                    onChange={(event) =>
                      setAnswers((current) => ({
                        ...current,
                        [entry.intervention_id]: event.target.value,
                      }))
                    }
                  />
                  <button
                    type="button"
                    style={{ marginTop: 8 }}
                    onClick={() => act(entry, "answer", { answer: answers[entry.intervention_id] ?? "" })}
                    disabled={resolve.isPending}
                  >
                    Answer
                  </button>
                </div>
              ) : null}
              <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
                {entry.requires_browser_handoff ? (
                  <>
                    <button
                      type="button"
                      onClick={() => openHost(entry)}
                      disabled={openBrowser.isPending}
                    >
                      Open browser
                    </button>
                    <button
                      type="button"
                      onClick={() => act(entry, "done_continue")}
                      disabled={resolve.isPending}
                    >
                      Done, continue
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => act(entry, "keep_control")}
                      disabled={resolve.isPending}
                    >
                      Keep control
                    </button>
                  </>
                ) : (
                  <button type="button" onClick={() => act(entry, "done_continue")} disabled={resolve.isPending}>
                    Continue
                  </button>
                )}
                <button
                  type="button"
                  className="secondary"
                  onClick={() => act(entry, "skip_application")}
                  disabled={resolve.isPending}
                >
                  Skip application
                </button>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
