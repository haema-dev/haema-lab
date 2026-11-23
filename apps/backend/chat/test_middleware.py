"""API 요청·응답 로그 미들웨어. DB·네트워크 없이 RequestFactory 로 돌린다."""

from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory, SimpleTestCase

from chat.middleware import RequestLogMiddleware

LOGGER = "chat.middleware"


class RequestLogMiddlewareTests(SimpleTestCase):
    def call(self, path, response=None, *, error=None, **headers):
        def view(request):
            if error:
                raise error
            return response

        request = RequestFactory().post(path, **headers)
        return RequestLogMiddleware(view)(request)

    def test_request_and_response_are_logged_once_each(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.call("/api/chat/conversations", JsonResponse({}, status=201), HTTP_X_USER_ID="1",
                      HTTP_X_REQUEST_ID="abc")
        self.assertEqual(len(logs.output), 2)
        self.assertIn("api req id=abc POST /api/chat/conversations user=1", logs.output[0])
        self.assertIn("api res id=abc POST /api/chat/conversations status=201 dur=", logs.output[1])
        self.assertTrue(logs.output[1].startswith("INFO"))

    def test_error_status_is_warning(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.call("/api/chat/conversations/1/messages", JsonResponse({"error": "x"}, status=429))
        self.assertTrue(logs.output[-1].startswith("WARNING"))
        self.assertIn("status=429", logs.output[-1])

    def test_exception_is_logged_as_500_and_reraised(self):
        with self.assertLogs(LOGGER, level="INFO") as logs, self.assertRaises(RuntimeError):
            self.call("/api/chat/conversations", error=RuntimeError("boom"))
        self.assertTrue(logs.output[-1].startswith("ERROR"))
        self.assertIn("status=500", logs.output[-1])

    def test_health_is_not_logged(self):
        with self.assertNoLogs(LOGGER, level="INFO"):
            self.call("/health", HttpResponse("ok"))
