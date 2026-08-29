export function EmptyState({ title, message }: { title: string; message: string }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      <p>{message}</p>
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return <div className="error">{message}</div>;
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <div className="loading">{label}</div>;
}
