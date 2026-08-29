import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { ScoreBar } from "./ScoreBar";
import { HealthDot } from "./HealthDot";

describe("ScoreBar", () => {
  it("renders a fill width proportional to the score", () => {
    const { container } = render(<ScoreBar score={0.5} />);
    const fill = container.querySelector(".score-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("50%");
  });

  it("clamps width at 100% when score exceeds max", () => {
    const { container } = render(<ScoreBar score={2} max={1} />);
    const fill = container.querySelector(".score-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("100%");
  });

  it("colors green at or above 0.75, yellow at 0.5-0.74, red below 0.5", () => {
    const cases: Array<[number, string]> = [
      [0.9, "var(--green)"],
      [0.6, "var(--yellow)"],
      [0.2, "var(--red)"],
    ];
    for (const [score, expected] of cases) {
      const { container } = render(<ScoreBar score={score} />);
      const fill = container.querySelector(".score-bar-fill") as HTMLElement;
      expect(fill.style.background).toBe(expected);
    }
  });
});

describe("HealthDot", () => {
  it("falls back to the unknown class for unrecognised states", () => {
    render(<HealthDot state={"bogus" as never} />);
    const dot = screen.getByTitle("bogus");
    expect(dot).toHaveClass("health-dot", "unknown");
  });

  it("maps a known state to its dedicated class", () => {
    render(<HealthDot state="healthy" />);
    expect(screen.getByTitle("healthy")).toHaveClass("health-dot", "healthy");
  });
});
