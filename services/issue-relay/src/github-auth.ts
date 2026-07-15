export const GITHUB_API_VERSION = "2026-03-10";
export const GITHUB_API_ROOT = "https://api.github.com";

const GITHUB_REPOSITORY = "tunnelforge";
const TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60;
const DEFAULT_GITHUB_TIMEOUT_MS = 10_000;
const IDENTIFIER_PATTERN = /^[1-9][0-9]*$/;
const PKCS8_PEM_PATTERN =
  /^-----BEGIN PRIVATE KEY-----\r?\n([A-Za-z0-9+/=\r\n]+)\r?\n-----END PRIVATE KEY-----$/;

export interface GitHubAuthEnv {
  readonly GITHUB_APP_ID: string;
  readonly GITHUB_APP_INSTALLATION_ID: string;
  readonly GITHUB_APP_PRIVATE_KEY: string;
}

export interface GitHubAuthOptions {
  readonly fetch?: typeof fetch;
  readonly now?: () => number;
  readonly timeoutMs?: number;
}

interface CachedInstallationToken {
  readonly identity: string;
  readonly token: string;
  readonly expiresAt: number;
}

interface PendingInstallationToken {
  readonly identity: string;
  readonly promise: Promise<CachedInstallationToken>;
}

export class GitHubAuthError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "GitHubAuthError";
    this.status = status;
  }
}

let cachedInstallationToken: CachedInstallationToken | undefined;
let pendingInstallationToken: PendingInstallationToken | undefined;

function requireIdentifier(value: string, name: string): void {
  if (!IDENTIFIER_PATTERN.test(value)) {
    throw new GitHubAuthError(`invalid ${name}`);
  }
}

function currentEpochSeconds(now: (() => number) | undefined): number {
  const value = now?.() ?? Math.floor(Date.now() / 1000);
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new GitHubAuthError("invalid GitHub authentication timestamp");
  }
  return value;
}

function timeoutMilliseconds(timeoutMs: number | undefined): number {
  const value = timeoutMs ?? DEFAULT_GITHUB_TIMEOUT_MS;
  if (!Number.isSafeInteger(value) || value <= 0 || value > 60_000) {
    throw new GitHubAuthError("invalid GitHub authentication timeout");
  }
  return value;
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replace(/=/g, "")
    .replace(/\+/g, "-")
    .replace(/\//g, "_");
}

function textToBase64Url(value: string): string {
  return bytesToBase64Url(new TextEncoder().encode(value));
}

function decodePkcs8Pem(pem: string): Uint8Array {
  const match = PKCS8_PEM_PATTERN.exec(pem.trim());
  if (match === null || match[1] === undefined) {
    throw new GitHubAuthError("GitHub App key must be PKCS#8 PRIVATE KEY PEM");
  }
  const encoded = match[1].replace(/\s/g, "");
  if (encoded.length === 0 || encoded.length % 4 !== 0) {
    throw new GitHubAuthError("GitHub App key must be valid PKCS#8 PEM");
  }
  try {
    return Uint8Array.from(atob(encoded), (character) =>
      character.charCodeAt(0),
    );
  } catch {
    throw new GitHubAuthError("GitHub App key must be valid PKCS#8 PEM");
  }
}

async function fetchWithTimeout(
  fetchImplementation: typeof fetch,
  input: RequestInfo | URL,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new GitHubAuthError("GitHub authentication request failed"));
      }, timeoutMs);
    });
    return await Promise.race([
      fetchImplementation(input, { ...init, signal: controller.signal }),
      timeout,
    ]);
  } catch (error) {
    if (error instanceof GitHubAuthError) {
      throw error;
    }
    throw new GitHubAuthError("GitHub authentication request failed");
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  }
}

async function readTokenResponseJson(
  response: Response,
  timeoutMs: number,
): Promise<unknown> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(
        () => reject(new GitHubAuthError("GitHub authentication request failed")),
        timeoutMs,
      );
    });
    return await Promise.race([response.json(), timeout]);
  } catch (error) {
    if (error instanceof GitHubAuthError) {
      throw error;
    }
    throw new GitHubAuthError("GitHub installation token response was invalid");
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  }
}

export async function createGitHubAppJwt(
  env: GitHubAuthEnv,
  options: Pick<GitHubAuthOptions, "now"> = {},
): Promise<string> {
  requireIdentifier(env.GITHUB_APP_ID, "GitHub App ID");
  const now = currentEpochSeconds(options.now);
  const keyBytes = decodePkcs8Pem(env.GITHUB_APP_PRIVATE_KEY);
  let key: CryptoKey;
  try {
    key = await crypto.subtle.importKey(
      "pkcs8",
      keyBytes,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["sign"],
    );
  } catch {
    throw new GitHubAuthError("GitHub App key must be valid PKCS#8 PEM");
  }
  const encodedHeader = textToBase64Url(
    JSON.stringify({ alg: "RS256", typ: "JWT" }),
  );
  const encodedClaims = textToBase64Url(
    JSON.stringify({
      iat: now - 60,
      exp: now + 540,
      iss: env.GITHUB_APP_ID,
    }),
  );
  const signingInput = `${encodedHeader}.${encodedClaims}`;
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(signingInput),
  );
  return `${signingInput}.${bytesToBase64Url(new Uint8Array(signature))}`;
}

async function requestInstallationToken(
  env: GitHubAuthEnv,
  identity: string,
  options: GitHubAuthOptions,
): Promise<CachedInstallationToken> {
  requireIdentifier(env.GITHUB_APP_INSTALLATION_ID, "GitHub App installation ID");
  const now = currentEpochSeconds(options.now);
  const jwt = await createGitHubAppJwt(env, { now: () => now });
  const response = await fetchWithTimeout(
    options.fetch ?? fetch,
    `${GITHUB_API_ROOT}/app/installations/${env.GITHUB_APP_INSTALLATION_ID}/access_tokens`,
    {
      method: "POST",
      redirect: "manual",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${jwt}`,
        "content-type": "application/json",
        "user-agent": "TunnelForge-Issue-Relay",
        "x-github-api-version": GITHUB_API_VERSION,
      },
      body: JSON.stringify({
        repositories: [GITHUB_REPOSITORY],
        permissions: { issues: "write" },
      }),
    },
    timeoutMilliseconds(options.timeoutMs),
  );
  if (response.status !== 201) {
    throw new GitHubAuthError(
      "GitHub installation token request was rejected",
      response.status,
    );
  }

  const payload = await readTokenResponseJson(
    response,
    timeoutMilliseconds(options.timeoutMs),
  );
  if (typeof payload !== "object" || payload === null) {
    throw new GitHubAuthError("GitHub installation token response was invalid");
  }
  const token = Reflect.get(payload, "token");
  const expiresAtValue = Reflect.get(payload, "expires_at");
  const expiresAt =
    typeof expiresAtValue === "string"
      ? Math.floor(Date.parse(expiresAtValue) / 1000)
      : Number.NaN;
  if (
    typeof token !== "string" ||
    token.length === 0 ||
    token.length > 4096 ||
    /[\s\u0000-\u001f\u007f]/.test(token) ||
    !Number.isSafeInteger(expiresAt) ||
    expiresAt <= now
  ) {
    throw new GitHubAuthError("GitHub installation token response was invalid");
  }
  return { identity, token, expiresAt };
}

export async function getInstallationToken(
  env: GitHubAuthEnv,
  forceRefresh = false,
  options: GitHubAuthOptions = {},
): Promise<string> {
  requireIdentifier(env.GITHUB_APP_ID, "GitHub App ID");
  requireIdentifier(env.GITHUB_APP_INSTALLATION_ID, "GitHub App installation ID");
  const now = currentEpochSeconds(options.now);
  const identity = `${env.GITHUB_APP_ID}:${env.GITHUB_APP_INSTALLATION_ID}`;
  if (
    !forceRefresh &&
    cachedInstallationToken?.identity === identity &&
    now < cachedInstallationToken.expiresAt - TOKEN_REFRESH_MARGIN_SECONDS
  ) {
    return cachedInstallationToken.token;
  }
  if (forceRefresh && cachedInstallationToken?.identity === identity) {
    cachedInstallationToken = undefined;
  }
  if (pendingInstallationToken?.identity === identity) {
    return (await pendingInstallationToken.promise).token;
  }

  const promise = requestInstallationToken(env, identity, options);
  pendingInstallationToken = { identity, promise };
  try {
    const token = await promise;
    cachedInstallationToken = token;
    return token.token;
  } finally {
    if (pendingInstallationToken?.promise === promise) {
      pendingInstallationToken = undefined;
    }
  }
}
