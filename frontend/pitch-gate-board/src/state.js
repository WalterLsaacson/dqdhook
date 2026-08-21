/** Shared UI state for pitch-gate-board. */

export const state = {
  goals: [],
  /** @type {"all"|"in_play"|"reversed"} */
  filter: "all",
  selectedKey: null,
  pollTimer: null,
  lastMeta: null,
  seenReversalKeys: new Set(),
  seededReversals: false,
};
