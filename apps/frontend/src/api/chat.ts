// src/api/chat.ts
// Django 채팅 API 클라이언트. 계약 원본: apps/backend/chat/views.py
//
// 브라우저 → Gateway(/api/**) → Django. 같은 출처라 경로만 쓴다.
// 로그인이 아직 없어 X-User-Id 는 Gateway 가 넣는다(클라이언트가 보내도 덮어씀).
// 로컬 개발(pnpm dev)은 vite.config.ts 의 프록시가 /api 를 백엔드로 넘긴다.

// import.meta.env 는 Vite 가 채운다. Vite 밖(테스트 스크립트 등)에서는 없으므로 ?. 로 읽는다.
export const API_BASE: string = import.meta.env?.VITE_API_BASE ?? '/api/chat';
export const MAX_QUESTION_CHARS = 300; // backend settings.MAX_QUESTION_CHARS

export type MessageStatus = 'pending' | 'processing' | 'done' | 'failed' | 'rejected';
export type MessageSource = 'cache' | 'generated' | 'fallback' | null;

export interface Verification {
  passed: boolean;
  reasons: string[];
  citations: string[];
  unsupported_facts: string[];
}

export interface ChatMessage {
  id: number;
  conversation_id: number;
  question: string | null;
  answer: string | null;
  status: MessageStatus;
  source: MessageSource;
  cited_chunk_ids: number[];
  verification: Verification | null;
  reason: string | null; // 실패·거절 사유 코드 (예: no_evidence)
  created_at: string;
  updated_at: string;
}

export interface Conversation {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface MessagePage {
  results: ChatMessage[];
  has_more: boolean;
  next_before_id: number | null;
}

export const FINAL_STATUSES: readonly MessageStatus[] = ['done', 'failed', 'rejected'];
export const isFinal = (status: MessageStatus) => FINAL_STATUSES.includes(status);

/** 상태코드와 서버 메시지를 담은 에러. 429·503 은 retryAfterSeconds 가 있을 수 있다. */
export class ApiError extends Error {
  readonly status: number;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, message: string, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

type FetchLike = typeof fetch;

export const NOT_API_MESSAGE = 'API 대신 다른 응답(HTML 등)이 왔습니다. Gateway 의 /api 라우트가 꺼져 있을 수 있습니다.';

async function request<T>(path: string, init: RequestInit = {}, fetchImpl: FetchLike = fetch): Promise<T> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, '서버에 연결할 수 없습니다. 네트워크를 확인해 주세요.');
  }
  if (response.status === 204) return undefined as T;

  // JSON 이 아닌 응답(HTML 등)은 본문을 읽지 않는다: Gateway 의 /api 라우트가 꺼져 프론트 index.html 이 온 경우.
  const isJson = (response.headers.get('Content-Type') ?? '').includes('application/json');
  let body: unknown = null;
  if (isJson) {
    try {
      body = await response.json();
    } catch {
      body = null;
    }
  }
  if (response.ok && body === null) {
    // 2xx 인데 API 응답이 아니다. 성공으로 넘기면 화면이 빈 값으로 깨지므로 에러로 바꾼다(재시도해도 같으니 5xx 가 아님).
    throw new ApiError(response.status, NOT_API_MESSAGE);
  }
  if (!response.ok) {
    const serverMessage =
      body && typeof body === 'object' && 'error' in body && typeof (body as { error: unknown }).error === 'string'
        ? (body as { error: string }).error
        : null;
    const retryAfter = Number(response.headers.get('Retry-After'));
    throw new ApiError(
      response.status,
      serverMessage ?? defaultMessage(response.status),
      Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
    );
  }
  return body as T;
}

function defaultMessage(status: number): string {
  if (status === 401) return '인증 정보가 없습니다.';
  if (status === 404) return 'API 를 찾을 수 없습니다. (Gateway 의 /api 라우트가 꺼져 있을 수 있습니다)';
  if (status === 429) return '처리 중인 질문이 너무 많습니다.';
  if (status >= 500) return '서버에 일시적인 문제가 있습니다. 잠시 후 다시 시도해 주세요.';
  return `요청이 거절되었습니다 (HTTP ${status}).`;
}

export const listConversations = (f?: FetchLike) =>
  request<{ results: Conversation[] }>('/conversations', {}, f).then((r) => r.results);

export const createConversation = (title = '', f?: FetchLike) =>
  request<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({ title }) }, f);

export const listMessages = (conversationId: number, limit = 50, f?: FetchLike) =>
  request<MessagePage>(`/conversations/${conversationId}/messages?limit=${limit}`, {}, f);

export const getMessage = (messageId: number, f?: FetchLike) => request<ChatMessage>(`/messages/${messageId}`, {}, f);

/** 질문을 보낸다. 202 로 pending 메시지가 돌아오고, 답은 pollMessage 로 기다린다. */
export function sendQuestion(conversationId: number, question: string, f?: FetchLike): Promise<ChatMessage> {
  const trimmed = question.trim();
  if (!trimmed) return Promise.reject(new ApiError(400, '질문을 입력해 주세요.'));
  if (trimmed.length > MAX_QUESTION_CHARS) {
    return Promise.reject(new ApiError(400, `질문은 ${MAX_QUESTION_CHARS}자 이하로 입력해 주세요.`));
  }
  return request<ChatMessage>(
    `/conversations/${conversationId}/messages`,
    { method: 'POST', body: JSON.stringify({ question: trimmed }) },
    f,
  );
}

export interface PollOptions {
  intervalMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
  onUpdate?: (message: ChatMessage) => void;
  fetchImpl?: FetchLike;
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 메시지가 done / failed / rejected 가 될 때까지 GET 으로 기다린다 (DB 조회 한 번, 큐·모델을 거치지 않음).
 * 생성은 실측 3~20초, 인프라 재시도가 겹치면 1분 이상 걸릴 수 있어 기본 제한은 10분.
 * 일시적인 네트워크·5xx 에러는 다음 주기에 다시 묻는다.
 */
export async function pollMessage(messageId: number, options: PollOptions = {}): Promise<ChatMessage> {
  const { intervalMs = 2000, timeoutMs = 600_000, signal, onUpdate, fetchImpl, sleep = defaultSleep } = options;
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (signal?.aborted) throw new ApiError(0, '중단되었습니다.');
    try {
      const message = await getMessage(messageId, fetchImpl);
      onUpdate?.(message);
      if (isFinal(message.status)) return message;
    } catch (error) {
      const transient = error instanceof ApiError && (error.status === 0 || error.status >= 500);
      if (!transient) throw error;
    }
    if (Date.now() >= deadline) throw new ApiError(0, '답변을 기다리는 시간이 너무 깁니다. 나중에 다시 확인해 주세요.');
    await sleep(intervalMs);
  }
}

/** 사유 코드를 사용자에게 보여줄 문장으로. 모르는 코드는 일반 문장. */
export function describeReason(message: Pick<ChatMessage, 'status' | 'reason'>): string | null {
  const code = message.reason ?? '';
  if (message.status === 'rejected') {
    if (code === 'verification_failed') return '답변이 근거 검증을 통과하지 못해 표시하지 않습니다.';
    if (code === 'empty_question') return '질문이 비어 있습니다.';
    return '답변을 표시할 수 없습니다.';
  }
  if (message.status === 'failed') {
    if (code === 'no_evidence') return '질문에 맞는 근거 문서를 찾지 못했습니다.';
    if (code.startsWith('infra')) return '모델 서버에 일시적인 문제가 있어 답하지 못했습니다. 잠시 후 다시 질문해 주세요.';
    if (code === 'internal_error') return '처리 중 오류가 발생했습니다.';
    return '답하지 못했습니다.';
  }
  return null;
}

/** created_at → updated_at (초). 끝난 메시지의 처리 시간. */
export function elapsedSeconds(message: Pick<ChatMessage, 'created_at' | 'updated_at'>): number {
  return Math.max(0, (Date.parse(message.updated_at) - Date.parse(message.created_at)) / 1000);
}
