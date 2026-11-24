"""API 가 내보내는 메시지 모양. 저장하지 않은 모델 인스턴스로 DB 없이 확인한다."""

from django.test import SimpleTestCase

from chat.models import Message
from chat.views import _message_dict


class MessagePayloadTests(SimpleTestCase):
    def test_reason_is_only_the_code_before_colon(self):
        m = Message(id=1, conversation_id=2, question="q", status=Message.Status.FAILED,
                    error="infra.ollama_unreachable: /embed: ollama_unreachable (HTTP 503): http://10.0.0.1")
        payload = _message_dict(m)
        self.assertEqual(payload["reason"], "infra.ollama_unreachable")
        self.assertNotIn("error", payload)  # 내부 메시지·주소는 내보내지 않는다

    def test_no_error_means_no_reason(self):
        m = Message(id=1, conversation_id=2, question="q", status=Message.Status.DONE, error=None)
        self.assertIsNone(_message_dict(m)["reason"])
        self.assertEqual(_message_dict(m)["cited_chunk_ids"], [])
