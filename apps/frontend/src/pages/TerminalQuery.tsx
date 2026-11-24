// src/pages/TerminalQuery.tsx
// 실제 RAG 질의 화면: 질문 → 202(pending) → 폴링 → done / rejected / failed.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Cpu, Plus, Radio, Send, ShieldCheck, ShieldAlert, Database, Clock } from 'lucide-react';
import {
  ApiError,
  MAX_QUESTION_CHARS,
  createConversation,
  describeReason,
  elapsedSeconds,
  isFinal,
  listConversations,
  listMessages,
  pollMessage,
  sendQuestion,
  type ChatMessage,
  type Conversation,
} from '../api/chat';
import '../styles/TerminalScanner.css'; // 상태바·버튼·커서 스타일을 같이 쓴다
import '../styles/TerminalQuery.css';

const STATUS_LABEL: Record<ChatMessage['status'], string> = {
  pending: 'QUEUED',
  processing: 'PROCESSING',
  done: 'DONE',
  rejected: 'REJECTED',
  failed: 'FAILED',
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.retryAfterSeconds ? `${error.message} (${error.retryAfterSeconds}초 뒤 다시 시도)` : error.message;
  }
  return '알 수 없는 오류가 발생했습니다.';
}

function MessageBlock({ message }: { message: ChatMessage }) {
  const final = isFinal(message.status);
  const reason = describeReason(message);
  const passed = message.verification?.passed;
  return (
    <article className={`query-item status-${message.status}`} data-testid={`message-${message.id}`}>
      <p className="query-question">&gt; {message.question}</p>

      <div className="query-meta">
        <span className={`query-badge status-${message.status}`}>{STATUS_LABEL[message.status]}</span>
        {message.source && (
          <span className="query-badge source">
            <Database size={10} /> {message.source === 'cache' ? 'CACHE' : message.source.toUpperCase()}
          </span>
        )}
        {final && passed !== undefined && (
          <span className={`query-badge ${passed ? 'verified' : 'unverified'}`}>
            {passed ? <ShieldCheck size={10} /> : <ShieldAlert size={10} />} {passed ? 'VERIFIED' : 'UNVERIFIED'}
          </span>
        )}
        {final && (
          <span className="query-badge time">
            <Clock size={10} /> {elapsedSeconds(message).toFixed(1)}s
          </span>
        )}
      </div>

      {!final && (
        <p className="query-waiting">
          // {message.status === 'pending' ? '대기열에서 순서를 기다리는 중' : '근거 검색과 답변 생성 중'}
          <span className="llm-cursor" />
        </p>
      )}
      {message.status === 'done' && message.answer && (
        <div className="query-answer">
          <div className="query-answer-header">
            <Cpu size={12} /> NODE_B RAG ANSWER
            {message.cited_chunk_ids.length > 0 && (
              <span className="query-citations">근거 {message.cited_chunk_ids.map((id) => `[${id}]`).join(' ')}</span>
            )}
          </div>
          <p className="query-answer-text">{message.answer}</p>
        </div>
      )}
      {reason && <p className="query-reason">! {reason}</p>}
    </article>
  );
}

export default function TerminalQuery() {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const polling = useRef(new Set<number>());
  const abort = useRef<AbortController | null>(null);
  const logEnd = useRef<HTMLDivElement>(null);

  const upsert = useCallback((message: ChatMessage) => {
    setMessages((prev) => {
      const index = prev.findIndex((m) => m.id === message.id);
      if (index === -1) return [...prev, message];
      const next = prev.slice();
      next[index] = message;
      return next;
    });
  }, []);

  const watch = useCallback(
    (message: ChatMessage) => {
      if (isFinal(message.status) || polling.current.has(message.id)) return;
      polling.current.add(message.id);
      pollMessage(message.id, { signal: abort.current?.signal, onUpdate: upsert })
        .catch((e) => {
          if (!abort.current?.signal.aborted) setError(errorText(e));
        })
        .finally(() => polling.current.delete(message.id));
    },
    [upsert],
  );

  const openConversation = useCallback(
    async (target: Conversation) => {
      setConversation(target);
      setMessages([]);
      const page = await listMessages(target.id);
      setMessages(page.results);
      page.results.forEach(watch); // 새로고침 전에 보낸, 아직 끝나지 않은 질문도 이어서 기다린다
    },
    [watch],
  );

  useEffect(() => {
    const controller = new AbortController();
    abort.current = controller;
    (async () => {
      try {
        const existing = await listConversations();
        // 개발 모드(StrictMode)는 effect 를 두 번 돌린다: 먼저 정리된 실행은 세션을 만들지 않는다(빈 사용자에게 세션 2개 방지).
        if (controller.signal.aborted) return;
        await openConversation(existing[0] ?? (await createConversation('터미널 세션')));
      } catch (e) {
        if (!controller.signal.aborted) setError(errorText(e));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [openConversation]);

  useEffect(() => {
    logEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages]);

  const submit = async () => {
    if (!conversation || sending) return;
    setSending(true);
    setError(null);
    try {
      const created = await sendQuestion(conversation.id, question);
      setQuestion('');
      upsert(created);
      watch(created);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSending(false);
    }
  };

  const newSession = async () => {
    setError(null);
    try {
      await openConversation(await createConversation('터미널 세션'));
    } catch (e) {
      setError(errorText(e));
    }
  };

  const remaining = MAX_QUESTION_CHARS - question.trim().length;
  const inFlight = messages.filter((m) => !isFinal(m.status)).length;

  return (
    <div className="scanner-container query-container">
      <div className="scanner-status-bar">
        <div className="status-bar-left">
          <span className="status-os">
            <Radio size={12} /> RAG_QUERY // VERIFIED ANSWERS ONLY
          </span>
          <span className="status-link">
            LINK: DJANGO_API {conversation ? `· SESSION #${conversation.id}` : ''}
          </span>
        </div>
        <div className="status-bar-right">
          <span className="status-battery">IN_FLIGHT: {inFlight}</span>
          <button type="button" className="query-new-session" onClick={newSession} disabled={loading}>
            <Plus size={12} /> NEW SESSION
          </button>
        </div>
      </div>

      <div className="query-log" aria-live="polite">
        {loading && <p className="query-waiting">// CONNECTING_TO_API...</p>}
        {!loading && messages.length === 0 && !error && (
          <p className="query-empty">// 질문을 입력하세요. 답변의 수치는 인용한 근거 문서와 대조해 검증합니다.</p>
        )}
        {messages.map((m) => (
          <MessageBlock key={m.id} message={m} />
        ))}
        <div ref={logEnd} />
      </div>

      {error && (
        <p className="query-error" role="alert">
          ERR: {error}
        </p>
      )}

      <form
        className="query-input-row"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <textarea
          className="query-input"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder="예: 기준금리는 무엇에 영향을 주나요?"
          rows={2}
          maxLength={MAX_QUESTION_CHARS + 50}
          disabled={!conversation}
          aria-label="질문"
        />
        <div className="query-input-side">
          <span className={`query-counter ${remaining < 0 ? 'over' : ''}`}>{remaining}</span>
          <button
            type="submit"
            className="scanner-btn query-send"
            disabled={!conversation || sending || !question.trim() || remaining < 0}
          >
            <Send size={16} /> {sending ? 'SENDING' : 'SEND'}
          </button>
        </div>
      </form>
    </div>
  );
}
