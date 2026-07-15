import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import {
  GITHUB_API_VERSION,
  createGitHubAppJwt,
  getInstallationToken,
  type GitHubAuthEnv,
} from "../src/github-auth";

const FIXED_NOW = 1_800_000_000;
const SYNTHETIC_TOKEN = "synthetic-installation-token";

let privateKeyPem: string;
let publicKey: CryptoKey;

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function pem(label: string, bytes: ArrayBuffer): string {
  const encoded = base64(new Uint8Array(bytes));
  const lines = encoded.match(/.{1,64}/g) ?? [];
  return `-----BEGIN ${label}-----\n${lines.join("\n")}\n-----END ${label}-----`;
}

function decodeBase64Url(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

function authEnv(appId = "123456"): GitHubAuthEnv {
  return {
    GITHUB_APP_ID: appId,
    GITHUB_APP_INSTALLATION_ID: "987654",
    GITHUB_APP_PRIVATE_KEY: privateKeyPem,
  };
}

function tokenResponse(token = SYNTHETIC_TOKEN, expiresAt = FIXED_NOW + 3600): Response {
  return Response.json(
    { token, expires_at: new Date(expiresAt * 1000).toISOString() },
    { status: 201 },
  );
}

beforeAll(async () => {
  const keys = (await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  )) as CryptoKeyPair;
  const exportedPrivateKey = await crypto.subtle.exportKey(
    "pkcs8",
    keys.privateKey,
  );
  privateKeyPem = pem("PRIVATE KEY", exportedPrivateKey as ArrayBuffer);
  publicKey = keys.publicKey;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GitHub App authentication", () => {
  it("signs an RS256 JWT with the bounded GitHub App claims", async () => {
    const jwt = await createGitHubAppJwt(authEnv(), { now: () => FIXED_NOW });
    const [encodedHeader, encodedClaims, encodedSignature] = jwt.split(".");
    if (encodedHeader === undefined || encodedClaims === undefined || encodedSignature === undefined) {
      throw new Error("expected a compact JWT");
    }

    expect(JSON.parse(new TextDecoder().decode(decodeBase64Url(encodedHeader)))).toEqual({
      alg: "RS256",
      typ: "JWT",
    });
    const claims = JSON.parse(
      new TextDecoder().decode(decodeBase64Url(encodedClaims)),
    ) as Record<string, unknown>;
    expect(claims).toEqual({ iat: FIXED_NOW - 60, exp: FIXED_NOW + 540, iss: "123456" });
    expect(Number(claims.exp)).toBeLessThanOrEqual(FIXED_NOW + 540);
    expect(
      await crypto.subtle.verify(
        "RSASSA-PKCS1-v1_5",
        publicKey,
        decodeBase64Url(encodedSignature),
        new TextEncoder().encode(`${encodedHeader}.${encodedClaims}`),
      ),
    ).toBe(true);
  });

  it("requests a repository-scoped installation token with Issues write only", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => tokenResponse(),
    );

    const token = await getInstallationToken(authEnv("123457"), true, {
      fetch: fetchMock as unknown as typeof fetch,
      now: () => FIXED_NOW,
    });

    expect(token).toBe(SYNTHETIC_TOKEN);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls[0];
    if (call === undefined) {
      throw new Error("expected an installation-token request");
    }
    const [url, init] = call;
    expect(String(url)).toBe(
      "https://api.github.com/app/installations/987654/access_tokens",
    );
    expect(init?.method).toBe("POST");
    expect(init?.redirect).toBe("manual");
    expect(new Headers(init?.headers).get("x-github-api-version")).toBe(
      GITHUB_API_VERSION,
    );
    expect(new Headers(init?.headers).get("authorization")).toMatch(
      /^Bearer [^.]+\.[^.]+\.[^.]+$/,
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      repositories: ["tunnelforge"],
      permissions: { issues: "write" },
    });
  });

  it("rejects an installation-token redirect without following it", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.redirect).toBe("manual");
      return new Response(null, {
        status: 302,
        headers: { location: "https://redirect.invalid/credential-target" },
      });
    });

    await expect(
      getInstallationToken(authEnv("123458"), true, {
        fetch: fetchMock as unknown as typeof fetch,
        now: () => FIXED_NOW,
      }),
    ).rejects.toMatchObject({ status: 302 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("caches tokens only until five minutes before expiry", async () => {
    let now = FIXED_NOW;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(tokenResponse("token-one", FIXED_NOW + 3600))
      .mockResolvedValueOnce(tokenResponse("token-two", FIXED_NOW + 7200));
    const env = authEnv("123458");
    const options = {
      fetch: fetchMock as unknown as typeof fetch,
      now: () => now,
    };

    expect(await getInstallationToken(env, true, options)).toBe("token-one");
    now = FIXED_NOW + 3299;
    expect(await getInstallationToken(env, false, options)).toBe("token-one");
    now = FIXED_NOW + 3300;
    expect(await getInstallationToken(env, false, options)).toBe("token-two");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("rejects PKCS#1 input instead of importing a different PEM format", async () => {
    await expect(
      createGitHubAppJwt(
        {
          ...authEnv("123459"),
          GITHUB_APP_PRIVATE_KEY:
            "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----",
        },
        { now: () => FIXED_NOW },
      ),
    ).rejects.toThrow("PKCS#8");
  });

  it("fails without logging the PEM, JWT, token, or upstream exception", async () => {
    const spies = [
      vi.spyOn(console, "debug").mockImplementation(() => undefined),
      vi.spyOn(console, "info").mockImplementation(() => undefined),
      vi.spyOn(console, "log").mockImplementation(() => undefined),
      vi.spyOn(console, "warn").mockImplementation(() => undefined),
      vi.spyOn(console, "error").mockImplementation(() => undefined),
    ];
    const fetchMock = vi.fn(async () => {
      throw new Error(`upstream ${SYNTHETIC_TOKEN}`);
    });

    await expect(
      getInstallationToken(authEnv("123460"), true, {
        fetch: fetchMock as unknown as typeof fetch,
        now: () => FIXED_NOW,
        timeoutMs: 25,
      }),
    ).rejects.toThrow("GitHub authentication request failed");
    for (const spy of spies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });

  it("times out a stalled installation-token response body", async () => {
    const stalledResponse = new Response(
      new ReadableStream({
        start() {
          // Headers are available, but the upstream JSON body never completes.
        },
      }),
      {
        status: 201,
        headers: { "content-type": "application/json" },
      },
    );
    const fetchMock = vi.fn(async () => stalledResponse);

    const outcome = await Promise.race([
      getInstallationToken(authEnv("123461"), true, {
        fetch: fetchMock as unknown as typeof fetch,
        now: () => FIXED_NOW,
        timeoutMs: 5,
      }).catch((error: unknown) => error),
      new Promise<"still_pending">((resolve) => {
        setTimeout(() => resolve("still_pending"), 50);
      }),
    ]);

    expect(outcome).toMatchObject({
      name: "GitHubAuthError",
      message: "GitHub authentication request failed",
    });
  });
});
