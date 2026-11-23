#!/usr/bin/env bash
# 질문 → 답변 경로가 돌 준비가 됐는지 클러스터에서 확인한다. 읽기만 하고 아무것도 바꾸지 않는다.
# 실행: kubectl 이 있는 VM 에서  bash scripts/preflight.sh
# 결과: 항목마다 OK / FAIL 한 줄. FAIL 이 하나라도 있으면 종료 코드 1.
set -u
NS=default
fails=0
ok()   { printf 'OK    %s\n' "$1"; }
fail() { printf 'FAIL  %s\n      -> %s\n' "$1" "$2"; fails=$((fails + 1)); }

# 1. backend-secret 의 REDIS_URL (없으면 새 API·워커 파드가 CreateContainerConfigError)
want='redis://cloud-redis-svc.default.svc.cluster.local:6379/0'
got=$(kubectl get secret backend-secret -n "$NS" -o jsonpath='{.data.REDIS_URL}' 2>/dev/null | base64 -d 2>/dev/null)
if [ "$got" = "$want" ]; then ok "backend-secret REDIS_URL"
else fail "backend-secret REDIS_URL" "Actions 에서 'Inject backend-secret'(secret.yaml) 실행"; fi

# 2. Redis 파드가 PING 에 답하는지
if [ "$(kubectl exec cloud-redis-0 -n "$NS" -- redis-cli ping 2>/dev/null)" = "PONG" ]; then ok "redis PONG"
else fail "redis PONG" "kubectl get pod cloud-redis-0 -n $NS / kubectl describe pod cloud-redis-0 -n $NS"; fi

# 3. ai 파드에서 Ollama 에 닿고, 설정한 두 모델이 받아져 있는지 (Tailscale 경유)
ai_out=$(kubectl exec deploy/cloud-ai -n "$NS" -- python -c '
import os, httpx
names = {m["name"] for m in httpx.get(os.environ["OLLAMA_BASE_URL"].rstrip("/") + "/api/tags", timeout=5).json()["models"]}
missing = [m for m in (os.environ["GENERATE_MODEL"], os.environ["EMBED_MODEL"]) if m not in names and m + ":latest" not in names]
print("missing=" + ",".join(missing) if missing else "ok")
' 2>&1 | tail -1)
if [ "$ai_out" = "ok" ]; then ok "ai -> ollama, models present"
else fail "ai -> ollama, models present" "$ai_out (OLLAMA_BASE_URL 도달·ollama pull 확인)"; fi

# 4. DB: chat 테이블(table.sql), 테스트 유저 1(Gateway 가 고정으로 넣는 id), 임베딩된 근거 청크
db_out=$(kubectl exec deploy/cloud-backend -n "$NS" -- python manage.py shell -c '
from django.db import connection
from django.contrib.auth import get_user_model
with connection.cursor() as c:
    c.execute("select to_regclass(%s), to_regclass(%s), to_regclass(%s)", ["chat_message", "source_chunk", "answer_cache"])
    if None in c.fetchone():
        print("tables=missing"); raise SystemExit
    c.execute("select count(*) from source_chunk where embedding is not null")
    chunks = c.fetchone()[0]
print("tables=ok user1=%s chunks=%d" % (get_user_model().objects.filter(pk=1).exists(), chunks))
' 2>&1 | tail -1)
case "$db_out" in
  "tables=ok"*) ok "db tables (table.sql)" ;;
  *) fail "db tables (table.sql)" "$db_out (운영 DB 에 apps/backend/table.sql 적용)" ;;
esac
case "$db_out" in
  *"user1=True"*) ok "db test user id=1" ;;
  *) fail "db test user id=1" "kubectl exec deploy/cloud-backend -n $NS -- python manage.py shell -c \"from django.contrib.auth.models import User; u,_=User.objects.get_or_create(id=1, defaults={'username':'test'}); u.set_unusable_password(); u.save()\"" ;;
esac
case "$db_out" in
  *"chunks=0"|"tables=missing"*|"") fail "db indexed chunks" "근거가 0개면 모든 질문이 no_evidence. python manage.py load_chunks 로 색인" ;;
  *"chunks="*) ok "db indexed chunks (${db_out##*chunks=})" ;;
  *) fail "db indexed chunks" "$db_out" ;;
esac

# 5. 워커 (master 머지 후에 생긴다)
if kubectl get deploy cloud-backend-worker -n "$NS" >/dev/null 2>&1; then
  ready=$(kubectl get deploy cloud-backend-worker -n "$NS" -o jsonpath='{.status.readyReplicas}')
  if [ "${ready:-0}" -ge 1 ]; then ok "worker running"; else fail "worker running" "kubectl logs deploy/cloud-backend-worker -n $NS"; fi
else
  printf 'SKIP  worker (아직 배포 전)\n'
fi

echo
[ "$fails" -eq 0 ] && echo "모두 통과" || echo "FAIL ${fails}개"
exit $(( fails > 0 ))
