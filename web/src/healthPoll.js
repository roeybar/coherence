// The engine health check: often while the engine is out of reach, rarely while it answers.
export const DOWN_SECONDS = 5;
export const UP_SECONDS = 30;

export const period = (down) => (down ? DOWN_SECONDS : UP_SECONDS);

// one second passed: the seconds left, and whether a check is due now
export function nextLeft(left, down) {
  const l = left - 1;
  if (l > 0) return { left: l, check: false };
  return { left: period(down), check: true };
}

// any failed answer counts as down
export const downAfter = (wasDown, r) => !(r && r.ok);
