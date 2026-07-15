import type {
  RelayCounterName,
  RelayObservability,
} from "./types";

export const RELAY_COUNTER_NAMES = [
  "report_accepted",
  "method_rejected",
  "media_type_rejected",
  "body_too_large",
  "json_rejected",
  "schema_rejected",
  "fingerprint_rejected",
  "internal_error",
] as const satisfies readonly RelayCounterName[];

const RELAY_COUNTER_SET = new Set<string>(RELAY_COUNTER_NAMES);

function emptyCounters(): Record<RelayCounterName, number> {
  return Object.fromEntries(
    RELAY_COUNTER_NAMES.map((name) => [name, 0]),
  ) as Record<RelayCounterName, number>;
}

export function createRelayObservability(): RelayObservability {
  const counters = emptyCounters();

  return Object.freeze({
    increment(counter: RelayCounterName): void {
      if (!RELAY_COUNTER_SET.has(counter)) {
        return;
      }
      counters[counter] += 1;
    },
    snapshot(): Readonly<Record<RelayCounterName, number>> {
      return Object.freeze({ ...counters });
    },
  });
}
