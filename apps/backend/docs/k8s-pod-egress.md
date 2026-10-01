# k8s Pod 외부 요청과 출발 IP

질문: k8s Pod에 배포하면, Pod에서 나가는 요청이 같은 대역대의 끝 IP만 다른 서버에 붙을 수 있나?

## 결론
붙을 수 있다. 단, 목적지가 보는 출발 IP는 Pod IP가 아니라 Pod가 떠 있는 **노드 IP**다(추정: CNI 미확인, 일반적인 SNAT/masquerade 기준).

## 근거
- Pod → 클러스터 밖 트래픽은 보통 노드 IP로 SNAT되어 나간다.
- 목적지가 노드와 같은 대역이면 L3 라우팅만 되면 붙는다. 기본 egress는 막혀 있지 않다. NetworkPolicy를 걸었을 때만 제한된다.
- Pod IP 대역은 재배포·재스케줄마다 바뀌므로 방화벽 허용 목록에 쓸 수 없다.

## 이 프로젝트에 적용
- DB(Node A postgres VM): `pg_hba.conf`와 방화벽에는 **Pod IP가 아니라 노드 IP(또는 노드 대역)** 를 허용한다.
- Ollama(Node B, Tailscale 경유): 요청이 Tailscale을 타려면 Pod가 올라간 노드에서 Tailscale 경로가 보여야 한다. 노드마다 다르면 실패할 수 있다.
- 출발 IP를 고정해야 하면:
  - nodeSelector로 특정 노드에 고정, 또는
  - Cilium egress gateway 등 egress IP 기능 사용(CNI가 Cilium일 때).

## 확인 필요
- 사용 중인 CNI (`kubectl get pods -n kube-system`)
- NetworkPolicy 적용 여부 (`kubectl get networkpolicy -A`)
- 실제 출발 IP 측정: Pod에서 목적지 서버 로그나 `ss`로 확인
