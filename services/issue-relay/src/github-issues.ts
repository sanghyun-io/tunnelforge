import {
  GITHUB_API_ROOT,
  GITHUB_API_VERSION,
  GitHubAuthError,
  getInstallationToken as requestInstallationToken,
  type GitHubAuthEnv,
  type GitHubAuthOptions,
} from "./github-auth";
import {
  fingerprintMarker,
  formatIssue,
  formatRecurrenceComment,
} from "./issue-format";
import type { ErrorReport } from "./types";

export const GITHUB_OWNER = "sanghyun-io";
export const GITHUB_REPOSITORY = "tunnelforge";
export const GITHUB_REPOSITORY_API =
  `${GITHUB_API_ROOT}/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}`;

const DEFAULT_GITHUB_TIMEOUT_MS = 10_000;

export interface GitHubIssueEnv extends GitHubAuthEnv {}

export type GitHubMutationKind = "create" | "comment";
export type GitHubIssueAction = "created" | "commented";
export type GitHubIssueErrorCode =
  | "authentication_failed"
  | "lookup_ambiguous"
  | "route_recovery_required"
  | "mutation_not_authorized"
  | "mutation_rejected"
  | "mutation_ambiguous";

export interface GitHubIssueResult {
  readonly action: GitHubIssueAction;
  readonly issueNumber: number;
  readonly issueUrl: string;
}

export type InstallationTokenProvider = (
  env: GitHubAuthEnv,
  forceRefresh: boolean,
  options?: GitHubAuthOptions,
) => Promise<string>;

export interface GitHubIssueRequestOptions extends GitHubAuthOptions {
  readonly env: GitHubIssueEnv;
  readonly getInstallationToken?: InstallationTokenProvider;
}

type MutationAuthorizer = (
  kind: GitHubMutationKind,
  retry: boolean,
) => Promise<boolean>;

export interface UpsertIssueOptions extends GitHubIssueRequestOptions {
  readonly issueNumber?: number;
  readonly recurrenceCount?: number;
  readonly authorizeMutation: MutationAuthorizer;
}

export class GitHubIssueError extends Error {
  readonly code: GitHubIssueErrorCode;
  readonly ambiguous: boolean;
  readonly status?: number;

  constructor(
    code: GitHubIssueErrorCode,
    ambiguous: boolean,
    status?: number,
  ) {
    super(code);
    this.name = "GitHubIssueError";
    this.code = code;
    this.ambiguous = ambiguous;
    this.status = status;
  }
}

function timeoutMilliseconds(timeoutMs: number | undefined): number {
  const value = timeoutMs ?? DEFAULT_GITHUB_TIMEOUT_MS;
  if (!Number.isSafeInteger(value) || value <= 0 || value > 60_000) {
    throw new GitHubIssueError("authentication_failed", false);
  }
  return value;
}

function requireIssueNumber(issueNumber: number): void {
  if (!Number.isSafeInteger(issueNumber) || issueNumber <= 0) {
    throw new TypeError("invalid GitHub issue number");
  }
}

function canonicalIssueUrl(issueNumber: number): string {
  requireIssueNumber(issueNumber);
  return `https://github.com/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/issues/${issueNumber}`;
}

async function fetchWithTimeout(
  fetchImplementation: typeof fetch,
  input: RequestInfo | URL,
  init: RequestInit,
  timeoutMs: number,
  mutation: boolean,
): Promise<Response> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(
          new GitHubIssueError(
            mutation ? "mutation_ambiguous" : "lookup_ambiguous",
            true,
          ),
        );
      }, timeoutMs);
    });
    return await Promise.race([
      fetchImplementation(input, { ...init, signal: controller.signal }),
      timeout,
    ]);
  } catch (error) {
    if (error instanceof GitHubIssueError) {
      throw error;
    }
    throw new GitHubIssueError(
      mutation ? "mutation_ambiguous" : "lookup_ambiguous",
      true,
    );
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  }
}

async function readJsonWithTimeout(
  response: Response,
  timeoutMs: number,
  classification: GitHubIssueError,
): Promise<unknown> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(classification), timeoutMs);
    });
    return await Promise.race([response.json(), timeout]);
  } catch (error) {
    if (error instanceof GitHubIssueError) {
      throw error;
    }
    throw classification;
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  }
}

async function githubRequest(
  options: GitHubIssueRequestOptions,
  url: string,
  init: RequestInit,
  mutationKind?: GitHubMutationKind,
  authorizeMutation?: MutationAuthorizer,
): Promise<Response> {
  const tokenProvider =
    options.getInstallationToken ?? requestInstallationToken;
  const mutation = mutationKind !== undefined;

  for (let attempt = 0; attempt < 2; attempt += 1) {
    let token: string;
    try {
      token = await tokenProvider(options.env, attempt === 1, {
        fetch: options.fetch,
        now: options.now,
        timeoutMs: options.timeoutMs,
      });
    } catch (error) {
      if (error instanceof GitHubAuthError) {
        throw new GitHubIssueError(
          "authentication_failed",
          false,
          error.status,
        );
      }
      throw new GitHubIssueError("authentication_failed", false);
    }

    if (mutationKind !== undefined) {
      if (authorizeMutation === undefined) {
        throw new GitHubIssueError("mutation_not_authorized", false);
      }
      let authorized: boolean;
      try {
        authorized = await authorizeMutation(
          mutationKind,
          attempt === 1,
        );
      } catch {
        throw new GitHubIssueError("mutation_not_authorized", false);
      }
      if (!authorized) {
        throw new GitHubIssueError("mutation_not_authorized", false);
      }
    }

    const headers = new Headers(init.headers);
    headers.set("accept", "application/vnd.github+json");
    headers.set("authorization", `Bearer ${token}`);
    headers.set("user-agent", "TunnelForge-Issue-Relay");
    headers.set("x-github-api-version", GITHUB_API_VERSION);
    if (init.body !== undefined && init.body !== null) {
      headers.set("content-type", "application/json");
    }
    const response = await fetchWithTimeout(
      options.fetch ?? fetch,
      url,
      { ...init, headers, redirect: "manual" },
      timeoutMilliseconds(options.timeoutMs),
      mutation,
    );
    if (response.status === 401 && attempt === 0) {
      continue;
    }
    return response;
  }
  throw new GitHubIssueError(
    mutation ? "mutation_rejected" : "lookup_ambiguous",
    false,
    401,
  );
}

export async function inspectRoutedIssue(
  options: GitHubIssueRequestOptions,
  fingerprint: string,
  issueNumber: number,
): Promise<void> {
  requireIssueNumber(issueNumber);
  const response = await githubRequest(
    options,
    `${GITHUB_REPOSITORY_API}/issues/${issueNumber}`,
    { method: "GET" },
  );
  if (response.status === 404) {
    throw new GitHubIssueError("route_recovery_required", false, 404);
  }
  if (!response.ok) {
    throw new GitHubIssueError(
      "lookup_ambiguous",
      response.status >= 500,
      response.status,
    );
  }

  const payload = await readJsonWithTimeout(
    response,
    timeoutMilliseconds(options.timeoutMs),
    new GitHubIssueError("lookup_ambiguous", true),
  );
  if (typeof payload !== "object" || payload === null) {
    throw new GitHubIssueError("lookup_ambiguous", true);
  }
  const remoteNumber = Reflect.get(payload, "number");
  const state = Reflect.get(payload, "state");
  const body = Reflect.get(payload, "body");
  if (
    remoteNumber !== issueNumber ||
    typeof state !== "string" ||
    typeof body !== "string" ||
    !body.includes(fingerprintMarker(fingerprint))
  ) {
    throw new GitHubIssueError("lookup_ambiguous", true);
  }
  if (state === "closed") {
    throw new GitHubIssueError("route_recovery_required", false);
  }
  if (state !== "open") {
    throw new GitHubIssueError("lookup_ambiguous", true);
  }
}

function classifyMutationResponse(response: Response): never {
  if (response.status >= 400 && response.status < 500) {
    throw new GitHubIssueError(
      "mutation_rejected",
      false,
      response.status,
    );
  }
  throw new GitHubIssueError(
    "mutation_ambiguous",
    true,
    response.status,
  );
}

async function createIssue(
  report: ErrorReport,
  fingerprint: string,
  options: UpsertIssueOptions,
): Promise<GitHubIssueResult> {
  const issue = formatIssue(report, fingerprint);
  const response = await githubRequest(
    options,
    `${GITHUB_REPOSITORY_API}/issues`,
    {
      method: "POST",
      body: JSON.stringify(issue),
    },
    "create",
    options.authorizeMutation,
  );
  if (response.status !== 201) {
    classifyMutationResponse(response);
  }

  const payload = await readJsonWithTimeout(
    response,
    timeoutMilliseconds(options.timeoutMs),
    new GitHubIssueError("mutation_ambiguous", true),
  );
  const issueNumber =
    typeof payload === "object" && payload !== null
      ? Reflect.get(payload, "number")
      : undefined;
  if (
    typeof issueNumber !== "number" ||
    !Number.isSafeInteger(issueNumber) ||
    issueNumber <= 0
  ) {
    throw new GitHubIssueError("mutation_ambiguous", true);
  }
  return {
    action: "created",
    issueNumber,
    issueUrl: canonicalIssueUrl(issueNumber),
  };
}

async function commentOnIssue(
  report: ErrorReport,
  fingerprint: string,
  options: UpsertIssueOptions,
  issueNumber: number,
): Promise<GitHubIssueResult> {
  await inspectRoutedIssue(options, fingerprint, issueNumber);
  const response = await githubRequest(
    options,
    `${GITHUB_REPOSITORY_API}/issues/${issueNumber}/comments`,
    {
      method: "POST",
      body: JSON.stringify({
        body: formatRecurrenceComment(report, options.recurrenceCount ?? 1),
      }),
    },
    "comment",
    options.authorizeMutation,
  );
  if (response.status !== 201) {
    classifyMutationResponse(response);
  }
  return {
    action: "commented",
    issueNumber,
    issueUrl: canonicalIssueUrl(issueNumber),
  };
}

export async function upsertIssue(
  report: ErrorReport,
  fingerprint: string,
  options: UpsertIssueOptions,
): Promise<GitHubIssueResult> {
  if (options.issueNumber === undefined) {
    return createIssue(report, fingerprint, options);
  }
  return commentOnIssue(
    report,
    fingerprint,
    options,
    options.issueNumber,
  );
}
