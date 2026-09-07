// 14 §6b: "a test asserts the UI renders a FAIL purely from the API's status field."
// These are the M0 equivalents: formatting is pure, and the lib layer contains no
// thresholds and no verdict logic.

import { describe, expect, it } from "vitest";
import { duration, shortSha, titleCase } from "../src/lib/format";

describe("format", () => {
  it("renders durations at readable precision", () => {
    expect(duration(450)).toBe("450ms");
    expect(duration(1500)).toBe("1.5s");
    expect(duration(125_000)).toBe("2m 5s");
  });

  it("shortens shas without inventing one", () => {
    expect(shortSha("9f2a1c4abcdef")).toBe("9f2a1c4");
  });

  it("titles check names for display only", () => {
    expect(titleCase("snowflake.grants")).toBe("Snowflake Grants");
  });
});
