export function ScoreBar({ score, max = 1 }: { score: number; max?: number }) {
  const pct = Math.min(100, (score / max) * 100);
  const color = score >= 0.75 ? "var(--green)" : score >= 0.5 ? "var(--yellow)" : "var(--red)";
  return (
    <div className="score-bar">
      <div className="score-bar-fill" style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}
