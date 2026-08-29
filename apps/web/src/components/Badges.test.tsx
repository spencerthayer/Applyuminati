import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { StateBadge, RecBadge, VerifyBadge, AppStateBadge } from "./Badges";

describe("Badges", () => {
  it("StateBadge maps a known state to its color class", () => {
    render(<StateBadge state="apply" />);
    expect(screen.getByText("apply")).toHaveClass("badge", "badge-green");
  });

  it("StateBadge falls back to badge-muted for unknown states", () => {
    render(<StateBadge state="totally-unknown-state" />);
    expect(screen.getByText("totally-unknown-state")).toHaveClass("badge", "badge-muted");
  });

  it("RecBadge and VerifyBadge render their value as text", () => {
    render(
      <>
        <RecBadge rec="skip" />
        <VerifyBadge state="gone" />
      </>,
    );
    expect(screen.getByText("skip")).toHaveClass("badge-red");
    expect(screen.getByText("gone")).toHaveClass("badge-red");
  });

  it("AppStateBadge delegates to StateBadge", () => {
    render(<AppStateBadge state="rejected" />);
    expect(screen.getByText("rejected")).toHaveClass("badge-red");
  });
});
