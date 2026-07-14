export const RELAY_MODES = ["off", "shadow", "canary", "active"] as const;
export type RelayMode = (typeof RELAY_MODES)[number];

const RELAY_MODE_SET = new Set<string>(RELAY_MODES);

export function parseRelayMode(value: string | undefined): RelayMode {
  if (value !== undefined && RELAY_MODE_SET.has(value)) {
    return value as RelayMode;
  }
  return "off";
}

export async function verifyCanaryAuthorization(
  authorization: string | undefined,
  token: string | undefined,
): Promise<boolean> {
  const encoder = new TextEncoder();
  const expected = `Bearer ${token ?? ""}`;
  const [providedDigest, expectedDigest] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(authorization ?? "")),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const provided = new Uint8Array(providedDigest);
  const wanted = new Uint8Array(expectedDigest);
  let difference = 0;
  for (let index = 0; index < wanted.length; index += 1) {
    difference |= provided[index] ^ wanted[index];
  }
  return difference === 0 && typeof token === "string" && token.length > 0;
}
