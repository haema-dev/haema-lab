/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API 경로 접두사. 기본 /api/chat (Gateway 가 Django 로 보냄) */
  readonly VITE_API_BASE?: string;
}
