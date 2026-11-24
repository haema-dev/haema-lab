"""Discord 알림: 보낼지 말지, 쿨다운, 수치 필드, 실패해도 예외 없음. 네트워크 없이 _dispatch/httpx 를 mock 한다."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings

from chat import notify

HOOK = "https://discord.test/api/webhooks/1/secret-token"


def message(**kw):
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    base = dict(pk=7, status="done", source="generated", created_at=now, updated_at=now + timedelta(seconds=19.6),
                retry_count=0, error=None, answer="답 [1]", cited_chunk_ids=[1], verification={"passed": True})
    return SimpleNamespace(**{**base, **kw})


TRACE = SimpleNamespace(
    elapsed=19.2, stages={"embed": 0.41, "search": 0.02, "generate": 18.7},
    embed={"load_s": 0.0, "total_s": 0.38},
    generate={"prompt_tokens": 412, "output_tokens": 61, "output_tok_s": 4.48, "prompt_tok_s": 71.3,
              "ttft_s": 5.9, "load_s": 0.1},
)


class NotifyTests(SimpleTestCase):
    def setUp(self):
        notify._last_error_at.clear()
        patcher = patch.object(notify, "_dispatch")
        self.dispatch = patcher.start()
        self.addCleanup(patcher.stop)

    @override_settings(DISCORD_WEBHOOK_URL="")
    def test_no_url_sends_nothing(self):
        self.assertFalse(notify.job_result(message(), TRACE))
        self.assertFalse(notify.error("k", "t"))
        self.dispatch.assert_not_called()

    @override_settings(DISCORD_WEBHOOK_URL=HOOK)
    def test_job_result_carries_numbers_not_question(self):
        self.assertTrue(notify.job_result(message(), TRACE))
        payload, url = self.dispatch.call_args.args
        self.assertEqual(url, HOOK)
        embed = payload["embeds"][0]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(embed["title"], "job 7 · done")
        self.assertEqual(fields["전체 (접수→끝)"], "19.6초")
        self.assertEqual(fields["프롬프트 토큰"], "412")
        self.assertEqual(fields["출력 토큰"], "61")
        self.assertEqual(fields["생성 속도"], "4.48 tok/s")
        self.assertEqual(fields["TTFT"], "5.90초")
        self.assertEqual(fields["생성"], "18.7초")
        self.assertEqual(fields["검증"], "통과")
        self.assertNotIn("질문", fields)  # 사용자 질문 본문은 보내지 않는다

    @override_settings(DISCORD_WEBHOOK_URL=HOOK)
    def test_missing_metrics_show_dash(self):
        notify.job_result(message(status="failed", source=None, error="infra.ollama_unreachable: x",
                                  answer=None, cited_chunk_ids=None, verification=None), None)
        fields = {f["name"]: f["value"] for f in self.dispatch.call_args.args[0]["embeds"][0]["fields"]}
        self.assertEqual((fields["사유"], fields["출력 토큰"], fields["검증"]), ("infra.ollama_unreachable", "-", "-"))

    @override_settings(DISCORD_WEBHOOK_URL=HOOK, DISCORD_NOTIFY_JOBS=False)
    def test_job_notifications_can_be_turned_off(self):
        self.assertFalse(notify.job_result(message(), TRACE))

    @override_settings(DISCORD_WEBHOOK_URL=HOOK, DISCORD_ERROR_COOLDOWN_SECONDS=300)
    def test_same_error_is_sent_once_per_cooldown(self):
        self.assertTrue(notify.error("infra.ollama_unreachable", "인프라 실패"))
        self.assertFalse(notify.error("infra.ollama_unreachable", "인프라 실패"))
        self.assertTrue(notify.error("worker.db_unavailable", "DB"))  # 다른 key 는 보낸다
        self.assertEqual(self.dispatch.call_count, 2)
        with patch.object(notify.time, "monotonic", return_value=notify.time.monotonic() + 301):
            self.assertTrue(notify.error("infra.ollama_unreachable", "인프라 실패"))


class PostTests(SimpleTestCase):
    def test_post_failures_never_raise_and_never_log_the_url(self):
        for side in (httpx.ConnectError("down"), None):
            with self.subTest(side=side), patch.object(notify.httpx, "post") as post, \
                    self.assertLogs("chat.notify", level="WARNING") as logs:
                if side:
                    post.side_effect = side
                else:
                    post.return_value = httpx.Response(429)
                notify._post({"content": "x"}, HOOK)  # 예외 없음
            self.assertFalse(any("secret-token" in line for line in logs.output))
