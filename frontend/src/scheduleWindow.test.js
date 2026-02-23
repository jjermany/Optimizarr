import { describe, expect, it } from 'vitest';

import { isWithinWindow } from './scheduleWindow';

describe('isWithinWindow', () => {
  it('treats same-day windows as half-open intervals [start, end)', () => {
    expect(isWithinWindow(9, 9, 17)).toBe(true);
    expect(isWithinWindow(16, 9, 17)).toBe(true);
    expect(isWithinWindow(17, 9, 17)).toBe(false);
  });

  it('treats overnight windows as half-open with exclusive end hour', () => {
    expect(isWithinWindow(22, 22, 6)).toBe(true);
    expect(isWithinWindow(2, 22, 6)).toBe(true);
    expect(isWithinWindow(6, 22, 6)).toBe(false);
    expect(isWithinWindow(12, 22, 6)).toBe(false);
  });
});
